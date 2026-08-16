"""Regression coverage for the standalone usage-guard hook."""

import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

import usage_guard


NOW = 2_000_000_000.0
SCRIPT_FILE = Path(usage_guard.__file__).resolve()


@pytest.fixture(autouse=True)
def isolate_guard_state(tmp_path, monkeypatch):
    """Keep every test away from the user's caches, flags, and script path."""
    paths = SimpleNamespace(
        cache=tmp_path / "config" / "ccstatus.json",
        legacy=tmp_path / "legacy" / "ccstatus.json",
        guard=tmp_path / "guard-flags",
        script=tmp_path / "installed hooks" / "usage_guard.py",
    )
    monkeypatch.setattr(usage_guard, "CACHE", str(paths.cache))
    monkeypatch.setattr(usage_guard, "LEGACY_CACHE", str(paths.legacy))
    monkeypatch.setattr(usage_guard, "GUARD_DIR", str(paths.guard))
    monkeypatch.setattr(usage_guard, "SCRIPT_PATH", str(paths.script))
    monkeypatch.setattr(usage_guard.time, "time", lambda: NOW)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    return paths


def _args(*, threshold=99.0, max_age=21600.0, json_output=False):
    return SimpleNamespace(threshold=threshold, max_age=max_age, json=json_output)


def _seed_cache(path, percent, *, now=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": percent, "resets_at": None}}}
        ),
        encoding="utf-8",
    )
    os.utime(path, (now, now))


def _seed_flag(paths, session_id="sess-1", *, mtime=NOW):
    paths.guard.mkdir(parents=True, exist_ok=True)
    flag = paths.guard / session_id
    flag.write_text("armed\n", encoding="utf-8")
    os.utime(flag, (mtime, mtime))
    return flag


def _hook_input(monkeypatch, *, session_id="sess-1", tool_name="Read"):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": session_id, "tool_name": tool_name})),
    )


def _session_start(
    paths,
    monkeypatch,
    *,
    percent,
    threshold=99.0,
    session_id="sess-1",
):
    _seed_cache(paths.cache, percent)
    _hook_input(monkeypatch, session_id=session_id, tool_name="")
    return usage_guard.mode_session_start(_args(threshold=threshold))


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("plain", "plain"),
        ("has space", "'has space'"),
        (r"C:\Python\python.exe", "C:/Python/python.exe"),
        (r"C:\Program Files\python.exe", "'C:/Program Files/python.exe'"),
    ],
)
def test_quote_arg(arg, expected):
    assert usage_guard._quote_arg(arg) == expected


@pytest.mark.parametrize(
    "arg",
    [
        'embedded"quote',
        "space and trailing slash\\",
        r'C:\path with spaces\"quoted"',
    ],
)
def test_quote_arg_is_bash_safe(arg):
    """These commands are pasted into a terminal that is bash on every
    platform, so no backslash may survive to be eaten as an escape."""
    quoted = usage_guard._quote_arg(arg)
    assert "\\" not in quoted
    assert shlex.split(quoted) == [arg.replace("\\", "/")]


def test_config_dir_nonempty_override_and_empty_default(tmp_path, monkeypatch):
    override = tmp_path / "override"
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    assert usage_guard._config_dir() == str(override)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert usage_guard._config_dir() == str(fake_home / ".claude")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert usage_guard._config_dir() == str(fake_home / ".claude")


def test_fresh_import_uses_claude_config_dir(tmp_path, monkeypatch):
    override = tmp_path / "override config"
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))

    spec = importlib.util.spec_from_file_location("usage_guard_fresh", SCRIPT_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.CACHE == str(override / "ccstatus.json")
    assert module.LEGACY_CACHE == str(fake_home / "legal" / "ccstatus.json")
    assert not (fake_home / ".claude").exists()


def test_subprocess_override_does_not_touch_default_paths(tmp_path):
    override = tmp_path / "override config"
    fake_home = tmp_path / "home"
    cache = override / "ccstatus.json"
    _seed_cache(cache, 20.0, now=os.path.getmtime(SCRIPT_FILE))
    os.utime(cache, None)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["CLAUDE_CONFIG_DIR"] = str(override)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_FILE), "--json"],
        cwd=SCRIPT_FILE.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["percent"] == 20.0
    assert not (fake_home / ".claude").exists()
    assert not (fake_home / "legal" / "ccstatus.json").exists()


def test_new_cache_wins_when_both_exist(isolate_guard_state):
    _seed_cache(isolate_guard_state.cache, 11.0)
    _seed_cache(isolate_guard_state.legacy, 88.0)

    gauges, stale = usage_guard.read_gauges(21600.0)

    assert usage_guard._cache_file() == str(isolate_guard_state.cache)
    assert gauges[0]["percent"] == 11.0
    assert stale is False


def test_malformed_new_cache_does_not_fall_back(isolate_guard_state):
    isolate_guard_state.cache.parent.mkdir(parents=True)
    isolate_guard_state.cache.write_text("not json", encoding="utf-8")
    os.utime(isolate_guard_state.cache, (NOW, NOW))
    _seed_cache(isolate_guard_state.legacy, 88.0)

    assert usage_guard.read_gauges(21600.0) == ([], False)


def test_legacy_only_cache_fallback(isolate_guard_state):
    _seed_cache(isolate_guard_state.legacy, 72.0)

    gauges, stale = usage_guard.read_gauges(21600.0)

    assert usage_guard._cache_file() == str(isolate_guard_state.legacy)
    assert gauges[0]["percent"] == 72.0
    assert stale is False


def test_neither_cache_exists_uses_new_path_in_diagnostic(
    isolate_guard_state, capsys
):
    result = usage_guard.mode_manual(_args(json_output=True))

    assert result == 2
    assert str(isolate_guard_state.cache) in capsys.readouterr().err
    assert usage_guard._cache_file() == str(isolate_guard_state.cache)


def test_manual_mode_binds_one_cache_path(
    isolate_guard_state, monkeypatch, capsys
):
    _seed_cache(isolate_guard_state.cache, 25.0)
    calls = []

    def select_once():
        calls.append(None)
        if len(calls) > 1:
            raise AssertionError("cache path was selected more than once")
        return str(isolate_guard_state.cache)

    monkeypatch.setattr(usage_guard, "_cache_file", select_once)

    assert usage_guard.mode_manual(_args(json_output=True)) == 0
    assert json.loads(capsys.readouterr().out)["percent"] == 25.0
    assert len(calls) == 1


@pytest.mark.parametrize("raw", ["{", "[]", '"scalar"', "123", "null"])
def test_non_object_hook_payload_fails_open_through_entrypoint(
    raw, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert usage_guard.read_hook_payload() == {}

    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_FILE), "--hook-json"])
    assert usage_guard.main() == 0
    assert capsys.readouterr().out == ""


def test_read_hook_payload_keeps_object(monkeypatch):
    payload = {"session_id": "sess-1", "tool_name": "Read"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert usage_guard.read_hook_payload() == payload


@pytest.mark.parametrize(
    "payload",
    [{}, {"session_id": "sess-1", "tool_name": "Read"}],
)
def test_missing_or_unarmed_hook_is_noop(payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        usage_guard,
        "read_gauges",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unarmed hook read the cache")
        ),
    )

    assert usage_guard.mode_hook_json(_args()) == 0
    assert capsys.readouterr().out == ""


def test_armed_fresh_over_threshold_ordinary_tool_is_denied(
    isolate_guard_state, monkeypatch, capsys
):
    _seed_cache(isolate_guard_state.cache, 99.0)
    flag = _seed_flag(isolate_guard_state)
    _hook_input(monkeypatch)

    assert usage_guard.mode_hook_json(_args()) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert flag.exists(), "denial must not auto-disarm the session"


def test_denial_names_escape_tools_and_exact_disarm_command(
    isolate_guard_state, monkeypatch, capsys
):
    _seed_cache(isolate_guard_state.cache, 98.0)
    _seed_flag(isolate_guard_state, session_id="session with space")
    _hook_input(monkeypatch, session_id="session with space")

    assert usage_guard.mode_hook_json(_args(threshold=97.5)) == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    expected = usage_guard._guard_command("--disarm", "session with space")

    assert "ScheduleWakeup" in reason
    assert "AskUserQuestion" in reason
    assert expected in reason
    assert "97.5%" in reason


def test_empty_executable_uses_platform_fallback_for_arm_and_disarm(
    isolate_guard_state, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "executable", "")
    assert _session_start(
        isolate_guard_state, monkeypatch, percent=95.0, threshold=98.0
    ) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert usage_guard._guard_command(
        "--arm", "sess-1", threshold=98.0
    ).startswith("python3 ")
    assert "python3 " in context

    _seed_cache(isolate_guard_state.cache, 99.0)
    _seed_flag(isolate_guard_state)
    _hook_input(monkeypatch)
    assert usage_guard.mode_hook_json(_args()) == 0
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert usage_guard._guard_command("--disarm", "sess-1").startswith("python3 ")
    assert "python3 " in reason
    assert usage_guard._python_interpreter("win32") == "python"


@pytest.mark.parametrize("tool_name", sorted(usage_guard.NEVER_DENY_TOOLS))
def test_never_deny_tool_escapes_real_deny_fixture(
    tool_name, isolate_guard_state, monkeypatch, capsys
):
    _seed_cache(isolate_guard_state.cache, 100.0)
    _seed_flag(isolate_guard_state)
    _hook_input(monkeypatch, tool_name=tool_name)
    monkeypatch.setattr(
        usage_guard,
        "read_gauges",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("never-deny tool read the cache")
        ),
    )

    assert usage_guard.mode_hook_json(_args()) == 0
    assert capsys.readouterr().out == ""


def test_hook_prunes_stale_flag_before_cache_read(
    isolate_guard_state, monkeypatch, capsys
):
    flag = _seed_flag(
        isolate_guard_state,
        mtime=NOW - usage_guard.FLAG_MAX_AGE_SECONDS - 1,
    )
    _hook_input(monkeypatch)
    monkeypatch.setattr(
        usage_guard,
        "read_gauges",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale flag reached the cache")
        ),
    )

    assert usage_guard.mode_hook_json(_args()) == 0
    assert not flag.exists()
    assert not isolate_guard_state.guard.exists()
    assert capsys.readouterr().out == ""


def test_armed_hook_refreshes_flag_then_later_inactivity_prunes_it(
    isolate_guard_state, monkeypatch, capsys
):
    clock = [NOW]
    monkeypatch.setattr(usage_guard.time, "time", lambda: clock[0])
    flag = _seed_flag(isolate_guard_state, mtime=NOW - 3600)
    _hook_input(monkeypatch, tool_name="ScheduleWakeup")

    assert usage_guard.mode_hook_json(_args()) == 0
    assert flag.stat().st_mtime == pytest.approx(NOW)
    assert capsys.readouterr().out == ""

    clock[0] = NOW + usage_guard.FLAG_MAX_AGE_SECONDS - 1
    usage_guard.prune_flags()
    assert flag.exists()

    clock[0] = NOW + usage_guard.FLAG_MAX_AGE_SECONDS + 1
    usage_guard.prune_flags()
    assert not flag.exists()
    assert not isolate_guard_state.guard.exists()


def test_armed_hook_refreshes_before_cache_blind_warning(
    isolate_guard_state, monkeypatch, capsys
):
    flag = _seed_flag(isolate_guard_state, mtime=NOW - 3600)
    _hook_input(monkeypatch)

    assert usage_guard.mode_hook_json(_args()) == 0
    assert flag.stat().st_mtime == pytest.approx(NOW)
    assert "cache stale/missing" in json.loads(capsys.readouterr().out)["systemMessage"]


@pytest.mark.parametrize(
    ("percent", "threshold"),
    [(99.0, 99.0), (100.0, 99.0), (85.0, 80.0)],
)
def test_session_start_at_or_above_threshold_is_exhausted_not_offered(
    percent, threshold, isolate_guard_state, monkeypatch, capsys
):
    assert _session_start(
        isolate_guard_state,
        monkeypatch,
        percent=percent,
        threshold=threshold,
    ) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]

    assert f"{threshold:g}%" in context
    assert "already exhausted" in context
    assert "Do not offer or arm" in context
    assert "--arm" not in context


def test_session_start_quiet_at_offer_percent(
    isolate_guard_state, monkeypatch, capsys
):
    assert _session_start(
        isolate_guard_state, monkeypatch, percent=usage_guard.OFFER_PERCENT
    ) == 0
    assert capsys.readouterr().out == ""


def test_session_start_offer_uses_custom_threshold_and_exact_arm_command(
    isolate_guard_state, monkeypatch, capsys
):
    assert _session_start(
        isolate_guard_state, monkeypatch, percent=95.0, threshold=98.0
    ) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]
    expected = usage_guard._guard_command(
        "--arm", "sess-1", threshold=98.0
    )

    assert expected in context
    assert "98%" in context
    assert "six hours of inactivity" in context
    assert "may survive a finished session" in context


def test_arm_disarm_round_trip_uses_threshold_and_cleans_directory(
    isolate_guard_state, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_FILE), "--arm", "sess-1", "--threshold", "97.5"],
    )
    assert usage_guard.main() == 0
    assert (isolate_guard_state.guard / "sess-1").exists()
    armed = capsys.readouterr().out
    assert ">=97.5%" in armed
    assert "six hours of inactivity" in armed
    assert "may survive a finished session" in armed

    assert usage_guard.mode_disarm("sess-1") == 0
    assert not isolate_guard_state.guard.exists()
    assert "disarmed" in capsys.readouterr().out


def test_manual_missing_cache_tells_agent_to_ask_user(
    isolate_guard_state, capsys
):
    assert usage_guard.mode_manual(_args()) == 2
    error = capsys.readouterr().err
    assert str(isolate_guard_state.cache) in error
    assert "ask the user to run `uv run config.py`" in error
