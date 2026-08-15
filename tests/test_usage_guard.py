"""First tests for usage_guard.py: arm-command quoting and the
SessionStart offer (no hooks, no network — everything monkeypatched)."""

import io
import json
import sys
from types import SimpleNamespace

import pytest

import usage_guard


@pytest.mark.parametrize(
    ("arg", "platform", "expected"),
    [
        ("plain", "linux", "plain"),
        ("has space", "linux", "'has space'"),
        ("plain", "win32", "plain"),
        ("has space", "win32", '"has space"'),
        (r"C:\Program Files\python.exe", "win32", '"C:\\Program Files\\python.exe"'),
    ],
)
def test_quote_arg(arg, platform, expected):
    assert usage_guard._quote_arg(arg, platform=platform) == expected


def _seed_cache(path, percent):
    path.write_text(
        json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": percent, "resets_at": None}}}
        ),
        encoding="utf-8",
    )


def _session_start(tmp_path, monkeypatch, *, percent, script_path, session_id="sess-1"):
    cache = tmp_path / "ccstatus.json"
    _seed_cache(cache, percent)
    monkeypatch.setattr(usage_guard, "CACHE", str(cache))
    monkeypatch.setattr(usage_guard, "GUARD_DIR", str(tmp_path / ".ccguard"))
    monkeypatch.setattr(usage_guard, "SCRIPT_PATH", script_path)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": session_id}))
    )
    return usage_guard.mode_session_start(SimpleNamespace(max_age=21600.0))


def test_mode_session_start_arm_cmd_uses_sys_executable(tmp_path, monkeypatch, capsys):
    script = str(tmp_path / "with space" / "usage_guard.py")
    assert _session_start(tmp_path, monkeypatch, percent=95.0, script_path=script) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    # built from sys.executable, quoted for this platform — never hardcoded
    assert usage_guard._quote_arg(sys.executable) in context
    if sys.executable != "/usr/bin/python3":
        assert "/usr/bin/python3" not in context
    # the spaced script path is quoted
    assert usage_guard._quote_arg(script) in context
    assert usage_guard._quote_arg(script) != script
    assert "--arm sess-1" in context


def test_mode_session_start_quiet_under_threshold(tmp_path, monkeypatch, capsys):
    script = str(tmp_path / "usage_guard.py")
    assert _session_start(tmp_path, monkeypatch, percent=50.0, script_path=script) == 0
    assert capsys.readouterr().out == ""
