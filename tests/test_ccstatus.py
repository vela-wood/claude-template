import builtins
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import ccstatus


def _fresh_import(name: str):
    spec = importlib.util.spec_from_file_location(name, ccstatus.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _temp_files(path: Path) -> list[Path]:
    return list(path.glob(".ccstatus-*"))


def test_fresh_import_uses_claude_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "claude config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    module = _fresh_import("ccstatus_config_override_test")

    assert module.CACHE == str(config_dir / "ccstatus.json")
    assert module.MEMORY_CACHE == str(config_dir / "ccstatus.mem")
    assert not config_dir.exists()


def test_write_cache_writes_verbatim_payload(tmp_path, monkeypatch):
    cache = tmp_path / "nested" / "ccstatus.json"
    payload = b'{"rate_limits":{"five_hour":{"used_percentage":12.5}}}'
    monkeypatch.setattr(ccstatus, "CACHE", str(cache))

    ccstatus.write_cache(payload)

    assert cache.read_bytes() == payload
    assert _temp_files(cache.parent) == []


def test_write_cache_throttles_recent_target(tmp_path, monkeypatch):
    cache = tmp_path / "ccstatus.json"
    original = b'{"rate_limits":{"five_hour":{"used_percentage":10}}}'
    cache.write_bytes(original)
    monkeypatch.setattr(ccstatus, "CACHE", str(cache))
    monkeypatch.setattr(ccstatus.time, "time", lambda: cache.stat().st_mtime + 5)

    ccstatus.write_cache(
        b'{"rate_limits":{"five_hour":{"used_percentage":90}}}'
    )

    assert cache.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        b'{"rateLimits":{}}',
        b'{"rate_limits_extra":{}}',
    ],
)
def test_write_cache_requires_exact_rate_limits_key_substring(
    tmp_path, monkeypatch, payload
):
    cache = tmp_path / "ccstatus.json"
    monkeypatch.setattr(ccstatus, "CACHE", str(cache))

    ccstatus.write_cache(payload)

    assert not cache.exists()


class _WriteProxy:
    def __init__(
        self, raw, *, maximum=None, error=None, fixed_result="not-set"
    ):
        self.raw = raw
        self.maximum = maximum
        self.error = error
        self.fixed_result = fixed_result
        self.closed = False

    def write(self, data):
        if self.error is not None:
            raise self.error
        if self.fixed_result != "not-set":
            return self.fixed_result
        if self.maximum is not None:
            data = data[: self.maximum]
        return self.raw.write(data)

    def close(self):
        self.raw.close()
        self.closed = True


def _patch_fdopen(monkeypatch, **proxy_options):
    real_fdopen = ccstatus.os.fdopen
    proxies = []

    def fdopen(fd, mode, buffering=0):
        assert mode == "wb"
        assert buffering == 0
        proxy = _WriteProxy(
            real_fdopen(fd, mode, buffering=buffering), **proxy_options
        )
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(ccstatus.os, "fdopen", fdopen)
    return proxies


def test_atomic_write_failure_preserves_target_and_removes_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "ccstatus.json"
    target.write_bytes(b"old")
    _patch_fdopen(monkeypatch, error=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        ccstatus._atomic_write_bytes(str(target), b"replacement")

    assert target.read_bytes() == b"old"
    assert _temp_files(tmp_path) == []
    assert set(tmp_path.iterdir()) == {target}


def test_atomic_base_exception_closes_file_and_removes_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "ccstatus.json"
    target.write_bytes(b"old")
    proxies = _patch_fdopen(monkeypatch, error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        ccstatus._atomic_write_bytes(str(target), b"replacement")

    assert len(proxies) == 1
    assert proxies[0].closed is True
    assert target.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == {target}


def test_atomic_fdopen_failure_closes_descriptor_and_removes_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "ccstatus.json"
    target.write_bytes(b"old")
    created_fds = []
    real_mkstemp = ccstatus.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created_fds.append(fd)
        return fd, name

    monkeypatch.setattr(ccstatus.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(
        ccstatus.os,
        "fdopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
    )

    with pytest.raises(OSError, match="fdopen failed"):
        ccstatus._atomic_write_bytes(str(target), b"replacement")

    assert len(created_fds) == 1
    with pytest.raises(OSError):
        os.fstat(created_fds[0])
    assert target.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == {target}


def test_atomic_replace_failure_preserves_target_and_removes_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "ccstatus.json"
    target.write_bytes(b"old")
    proxies = _patch_fdopen(monkeypatch)

    def fail_replace(source, destination):
        assert len(proxies) == 1
        assert proxies[0].closed is True
        assert Path(source).parent == tmp_path
        assert destination == str(target)
        raise OSError("replace failed")

    monkeypatch.setattr(ccstatus.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        ccstatus._atomic_write_bytes(str(target), b"replacement")

    assert target.read_bytes() == b"old"
    assert _temp_files(tmp_path) == []
    assert set(tmp_path.iterdir()) == {target}


def test_atomic_write_loops_until_short_writes_complete(tmp_path, monkeypatch):
    target = tmp_path / "ccstatus.json"
    payload = b"a payload longer than three bytes"
    _patch_fdopen(monkeypatch, maximum=3)

    ccstatus._atomic_write_bytes(str(target), payload)

    assert target.read_bytes() == payload
    assert _temp_files(tmp_path) == []


@pytest.mark.parametrize("write_result", [None, 0, -1])
def test_atomic_nonprogress_write_does_not_replace_target(
    tmp_path, monkeypatch, write_result
):
    target = tmp_path / "ccstatus.json"
    target.write_bytes(b"old")
    _patch_fdopen(monkeypatch, fixed_result=write_result)

    with pytest.raises(OSError, match="made no progress"):
        ccstatus._atomic_write_bytes(str(target), b"replacement")

    assert target.read_bytes() == b"old"
    assert _temp_files(tmp_path) == []


def test_render_line_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(ccstatus, "_seg_free_memory", lambda: None)
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 50},
            "model": {"display_name": "Sonnet"},
            "rate_limits": {"five_hour": {"used_percentage": 12.5}},
            "cwd": str(tmp_path),
        }
    ).encode()

    rendered = ccstatus.render_line(payload)

    assert "[█████░░░░░] 50%" in rendered
    assert "Sonnet" in rendered
    assert "5h 12.5%" in rendered


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"null", b'"text"'])
def test_render_line_malformed_or_non_object_is_empty(payload):
    assert ccstatus.render_line(payload) == ""


def test_cp1252_stdout_is_reconfigured_to_utf8(tmp_path):
    config_dir = tmp_path / "config"
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["PYTHONIOENCODING"] = "cp1252"
    payload = b'{"context_window":{"used_percentage":50}}'

    result = subprocess.run(
        [sys.executable, str(Path(ccstatus.__file__).resolve())],
        input=payload,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    rendered = result.stdout.decode("utf-8")
    assert rendered
    assert "[█████░░░░░] 50%" in rendered
    assert result.stderr == b""
    assert not (config_dir / "ccstatus.json").exists()


def _seed_memory_cache(path: Path, used, total, *, mtime=None):
    path.write_text(
        json.dumps({"used_bytes": used, "total_bytes": total}), encoding="utf-8"
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_fresh_memory_cache_hit_avoids_recomputation(tmp_path, monkeypatch):
    cache = tmp_path / "ccstatus.mem"
    _seed_memory_cache(cache, 10, 20, mtime=1000)
    monkeypatch.setattr(ccstatus, "MEMORY_CACHE", str(cache))
    monkeypatch.setattr(ccstatus.time, "time", lambda: 1010)
    monkeypatch.setattr(
        ccstatus,
        "_macos_vm_stat",
        lambda: pytest.fail("fresh cache must avoid vm_stat"),
    )

    assert ccstatus._macos_memory_usage() == (10, 20)


def test_stale_memory_cache_recomputes(tmp_path, monkeypatch):
    cache = tmp_path / "ccstatus.mem"
    _seed_memory_cache(cache, 10, 20, mtime=1000)
    monkeypatch.setattr(ccstatus, "MEMORY_CACHE", str(cache))
    monkeypatch.setattr(ccstatus.time, "time", lambda: 1031)
    monkeypatch.setattr(ccstatus, "_macos_vm_stat", lambda: (30, 40))

    assert ccstatus._macos_memory_usage() == (30, 40)
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "used_bytes": 30,
        "total_bytes": 40,
    }


def test_memory_cache_fstats_the_open_file_during_path_replacement(
    tmp_path, monkeypatch
):
    cache = tmp_path / "ccstatus.mem"
    replacement = tmp_path / "stale.mem"
    _seed_memory_cache(cache, 10, 20, mtime=1030)
    _seed_memory_cache(replacement, 1, 2, mtime=1000)
    monkeypatch.setattr(ccstatus, "MEMORY_CACHE", str(cache))
    monkeypatch.setattr(ccstatus.time, "time", lambda: 1031)
    monkeypatch.setattr(ccstatus, "_macos_vm_stat", lambda: (30, 40))
    real_open = builtins.open
    replaced = False

    def racing_open(path, *args, **kwargs):
        nonlocal replaced
        if os.fspath(path) == str(cache) and not replaced:
            replaced = True
            os.replace(replacement, cache)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", racing_open)

    assert ccstatus._macos_memory_usage() == (30, 40)
    assert replaced is True


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"used_bytes":true,"total_bytes":20}',
        '{"used_bytes":-1,"total_bytes":20}',
        '{"used_bytes":21,"total_bytes":20}',
        '{"used_bytes":0,"total_bytes":0}',
    ],
)
def test_malformed_or_invalid_memory_cache_recomputes(
    tmp_path, monkeypatch, content
):
    cache = tmp_path / "ccstatus.mem"
    cache.write_text(content, encoding="utf-8")
    monkeypatch.setattr(ccstatus, "MEMORY_CACHE", str(cache))
    monkeypatch.setattr(ccstatus.time, "time", lambda: cache.stat().st_mtime + 1)
    monkeypatch.setattr(ccstatus, "_macos_vm_stat", lambda: (30, 40))

    assert ccstatus._macos_memory_usage() == (30, 40)


def test_memory_cache_write_failure_keeps_new_value(tmp_path, monkeypatch):
    cache = tmp_path / "missing" / "ccstatus.mem"
    monkeypatch.setattr(ccstatus, "MEMORY_CACHE", str(cache))
    monkeypatch.setattr(ccstatus, "_macos_vm_stat", lambda: (30, 40))

    def fail_write(used, total):
        raise OSError("read-only")

    monkeypatch.setattr(ccstatus, "_write_memory_cache", fail_write)

    assert ccstatus._macos_memory_usage() == (30, 40)


def test_macos_vm_stat_parsing(monkeypatch):
    output = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               40.
Pages active:                             10.
Pages inactive:                           20.
Pages wired down:                          2.
Pages occupied by compressor:              3.
"""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=output)

    def fake_sysconf(name):
        return {"SC_PHYS_PAGES": 100, "SC_PAGE_SIZE": 4096}[name]

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ccstatus.os, "sysconf", fake_sysconf)

    assert ccstatus._macos_vm_stat() == (15 * 4096, 100 * 4096)
    assert calls == [
        (
            (["/usr/bin/vm_stat"],),
            {"capture_output": True, "text": True, "timeout": 1},
        )
    ]


class _FakeGlobalMemoryStatusEx:
    def __init__(self, *, succeeds=True, total=0, available=0):
        self.succeeds = succeeds
        self.total = total
        self.available = available
        self.dw_length = None

    def __call__(self, pointer):
        status = ctypes.cast(
            pointer, ctypes.POINTER(ccstatus.MEMORYSTATUSEX)
        ).contents
        self.dw_length = status.dwLength
        status.ullTotalPhys = self.total
        status.ullAvailPhys = self.available
        return int(self.succeeds)


def _install_fake_kernel32(monkeypatch, call):
    windll = SimpleNamespace(
        kernel32=SimpleNamespace(GlobalMemoryStatusEx=call)
    )
    monkeypatch.setattr(ccstatus.ctypes, "windll", windll, raising=False)


def test_windows_global_memory_status_success(monkeypatch):
    call = _FakeGlobalMemoryStatusEx(total=16 * 2**30, available=6 * 2**30)
    _install_fake_kernel32(monkeypatch, call)

    assert ctypes.sizeof(ccstatus.MEMORYSTATUSEX) == 64
    assert ccstatus._windows_memory_usage() == (10 * 2**30, 16 * 2**30)
    assert call.dw_length == ctypes.sizeof(ccstatus.MEMORYSTATUSEX)


def test_windows_global_memory_status_failure_drops_only_memory(
    monkeypatch
):
    call = _FakeGlobalMemoryStatusEx(succeeds=False)
    _install_fake_kernel32(monkeypatch, call)
    monkeypatch.setattr(ccstatus.sys, "platform", "win32")
    payload = b'{"context_window":{"used_percentage":50}}'

    rendered = ccstatus.render_line(payload)

    assert "[█████░░░░░] 50%" in rendered
    assert call.dw_length == ctypes.sizeof(ccstatus.MEMORYSTATUSEX)
