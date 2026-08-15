"""Pure-helper tests for setup_claude.py (no TUI, no network)."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

import repo_settings
import setup_claude
from setup_claude import (
    BuildResult,
    SetupError,
    WriteResult,
    build_statusline_command,
    count_commits_behind,
    detect_python3,
    detect_renderer,
    hub_status,
    merge_local_settings,
    organization_choices,
    select_organization,
    statusline_settings,
    summarize_env_write,
    upstream_ref,
    _quote_for_platform,
)


# ---------------------------------------------------------------------------
# build_statusline_command / _quote_for_platform
# ---------------------------------------------------------------------------


def test_build_command_render_posix():
    assert build_statusline_command(
        "/usr/bin/python3", Path("/repo"), None, platform="linux"
    ) == "/usr/bin/python3 /repo/.claude/hooks/ccstatus-tee.py --render"


def test_build_command_renderer_posix_quotes_spaces():
    cmd = build_statusline_command(
        "/usr/bin/python3",
        Path("/my repo"),
        "/my repo/.statusline/node_modules/.bin/ccstatusline",
        platform="linux",
    )
    assert cmd == (
        "/usr/bin/python3 '/my repo/.claude/hooks/ccstatus-tee.py' | "
        "'/my repo/.statusline/node_modules/.bin/ccstatusline'"
    )


def test_build_command_windows_double_quotes():
    cmd = build_statusline_command(
        r"C:\Program Files\Python\python.exe",
        Path("/repo x"),
        None,
        platform="win32",
    )
    assert cmd.startswith('"C:\\Program Files\\Python\\python.exe" ')
    assert cmd.endswith(" --render")
    assert "'" not in cmd


def test_quote_for_platform():
    assert _quote_for_platform("plain", "linux") == "plain"
    assert _quote_for_platform("has space", "linux") == "'has space'"
    assert _quote_for_platform("has space", "win32") == '"has space"'


# ---------------------------------------------------------------------------
# detect_python3 / detect_renderer
# ---------------------------------------------------------------------------


def test_detect_python3_filters_venv_entries(monkeypatch):
    monkeypatch.setattr(setup_claude.sys, "platform", "linux")
    venv = "/repo/.venv"
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv(
        "PATH",
        ":".join([f"{venv}/bin", "/somewhere/.venv/bin", "/usr/local/bin", "/usr/bin"]),
    )
    seen = {}

    def fake_which(name, path=None):
        seen["name"] = name
        seen["path"] = path
        return "/usr/local/bin/python3"

    monkeypatch.setattr(setup_claude.shutil, "which", fake_which)
    assert detect_python3() == "/usr/local/bin/python3"
    assert seen["name"] == "python3"
    assert ".venv" not in seen["path"]
    assert "/usr/local/bin" in seen["path"] and "/usr/bin" in seen["path"]


def test_detect_python3_posix_fallback(monkeypatch):
    monkeypatch.setattr(setup_claude.sys, "platform", "linux")
    monkeypatch.setattr(setup_claude.shutil, "which", lambda *a, **k: None)
    assert detect_python3() == "/usr/bin/python3"


def test_detect_renderer(tmp_path):
    assert detect_renderer(tmp_path) is None
    exe = tmp_path / ".statusline" / "node_modules" / ".bin" / "ccstatusline"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert detect_renderer(tmp_path) == str(exe)


# ---------------------------------------------------------------------------
# organization helpers / summarize_env_write
# ---------------------------------------------------------------------------


def test_organization_choices():
    payload = {
        "organizations": [
            {"organization_name": "Acme", "organization_id": "org-1"},
            {"organization_name": "  ", "organization_id": None},
        ]
    }
    assert organization_choices(payload) == [
        ("Acme", "org-1"),
        ("Organization 2", "<missing organization_id>"),
    ]
    assert organization_choices({}) == []
    assert organization_choices({"organizations": None}) == []
    with pytest.raises(SetupError):
        organization_choices({"organizations": "nope"})
    with pytest.raises(SetupError):
        organization_choices({"organizations": ["nope"]})


def test_select_organization():
    payload = {"k": 1, "organizations": [{"organization_id": "a"}, {"organization_id": "b"}]}
    selected = select_organization(payload, 1)
    assert selected["organizations"] == [{"organization_id": "b"}]
    assert selected["k"] == 1
    assert payload["organizations"] != selected["organizations"]  # original intact
    with pytest.raises(SetupError):
        select_organization(payload, 2)
    with pytest.raises(SetupError):
        select_organization({"organizations": None}, 0)


def test_summarize_env_write():
    text = summarize_env_write(
        BuildResult(env_values={}, skipped_null_keys=("N",)),
        WriteResult(
            appended_new_keys=("A", "B"),
            appended_conflicting_keys=("C",),
            skipped_existing_keys=("D",),
        ),
    )
    assert "2 new key(s)" in text
    assert "1 conflicting value(s) appended" in text
    assert "1 already set" in text
    assert "1 null value(s) skipped" in text


# ---------------------------------------------------------------------------
# statusline_settings / merge_local_settings
# ---------------------------------------------------------------------------


def test_statusline_settings(tmp_path):
    path = tmp_path / "settings.local.json"
    assert statusline_settings(path) is None
    path.write_text("{malformed", encoding="utf-8")
    assert statusline_settings(path) is None
    path.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "x", "padding": 0}}),
        encoding="utf-8",
    )
    assert statusline_settings(path) == "x"


def test_merge_local_settings_preserves_keys_and_skips_identical(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["X"]}, "prefersReducedMotion": False}),
        encoding="utf-8",
    )
    assert merge_local_settings(path, "cmd one") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["X"]}
    assert data["prefersReducedMotion"] is False
    assert data["statusLine"] == {"type": "command", "command": "cmd one", "padding": 0}
    # identical → skip
    assert merge_local_settings(path, "cmd one") is False
    # different → rewrite
    assert merge_local_settings(path, "cmd two") is True
    assert statusline_settings(path) == "cmd two"


def test_merge_local_settings_creates_missing_file(tmp_path):
    path = tmp_path / "sub" / "settings.local.json"
    assert merge_local_settings(path, "cmd") is True
    assert statusline_settings(path) == "cmd"


@pytest.mark.parametrize("content", ["{broken", "[1]"])
def test_merge_local_settings_never_clobbers_malformed(tmp_path, content):
    path = tmp_path / "settings.local.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SetupError):
        merge_local_settings(path, "cmd")
    assert path.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Update check (subprocess monkeypatched)
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, returncode, stdout):
        self.returncode = None
        self._returncode = returncode
        self._stdout = stdout

    async def communicate(self):
        self.returncode = self._returncode
        return self._stdout.encode(), b""

    def kill(self):
        self.returncode = -9


def _fake_git(monkeypatch, responses):
    """responses: {git subcommand: (returncode, stdout)}. Records calls."""
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env")})
        rc, out = responses[argv[3]]
        return FakeProc(rc, out)

    monkeypatch.setattr(setup_claude.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_upstream_ref_none_without_upstream_and_no_fetch(monkeypatch):
    calls = _fake_git(monkeypatch, {"rev-parse": (128, "")})
    assert asyncio.run(count_commits_behind(Path("/repo"))) is None
    assert [c["argv"][3] for c in calls] == ["rev-parse"]  # no fetch ever


def test_count_commits_behind_by_n(monkeypatch):
    calls = _fake_git(
        monkeypatch,
        {
            "rev-parse": (0, "origin/main"),
            "fetch": (0, ""),
            "rev-list": (0, "7"),
        },
    )
    assert asyncio.run(count_commits_behind(Path("/repo"))) == 7
    fetch = next(c for c in calls if c["argv"][3] == "fetch")
    assert fetch["argv"][4:] == ("--quiet", "origin", "main")  # exact branch, never full refspec
    assert all(c["env"]["GIT_TERMINAL_PROMPT"] == "0" for c in calls)


@pytest.mark.parametrize(
    "responses",
    [
        {"rev-parse": (0, "origin/main"), "fetch": (1, ""), "rev-list": (0, "1")},
        {"rev-parse": (0, "origin/main"), "fetch": (0, ""), "rev-list": (1, "")},
        {"rev-parse": (0, "origin/main"), "fetch": (0, ""), "rev-list": (0, "garbage")},
        {"rev-parse": (0, "nodashslash")},
    ],
)
def test_count_commits_behind_failure_modes_return_none(monkeypatch, responses):
    _fake_git(monkeypatch, responses)
    assert asyncio.run(count_commits_behind(Path("/repo"))) is None


def test_upstream_ref_parses_remote_and_branch(monkeypatch):
    _fake_git(monkeypatch, {"rev-parse": (0, "origin/feature/x")})
    assert asyncio.run(upstream_ref(Path("/repo"))) == ("origin", "feature/x")


def test_upstream_ref_none_when_git_missing(monkeypatch):
    async def boom(*argv, **kwargs):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(setup_claude.asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(upstream_ref(Path("/repo"))) is None


# ---------------------------------------------------------------------------
# hub_status
# ---------------------------------------------------------------------------


def test_hub_status_rows_independent_on_malformed_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("{broken", encoding="utf-8")
    rows = hub_status(tmp_path)
    assert set(rows) == {"env", "sidecar", "statusline"}
    assert "settings.json invalid" in rows["sidecar"]
    assert "fix or delete it" in rows["sidecar"]
    assert rows["env"] == "not configured"
    assert rows["statusline"] == "not configured"


def test_hub_status_configured_statusline(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    local = tmp_path / ".claude" / "settings.local.json"
    recommended = setup_claude.build_statusline_command(
        detect_python3(), tmp_path, None
    )
    merge_local_settings(local, recommended)
    assert hub_status(tmp_path)["statusline"] == "configured"
    merge_local_settings(local, "something else")
    assert hub_status(tmp_path)["statusline"] == "differs from recommended"
