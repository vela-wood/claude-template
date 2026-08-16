"""Pure-helper tests for the setup package (config/) (no TUI, no network)."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fsio
import repo_settings
from config import app as config_app
from config import common as config_common
from config import guard as config_guard
from config import ocr as config_ocr
from config import statusline as config_statusline
from config.app import (
    count_commits_behind,
    hub_status,
    install_statusline_and_guard,
    upstream_ref,
)
from config.common import (
    SetupError,
    local_settings_path,
    posix_path,
    shell_quote,
    user_claude_dir,
    write_settings,
)
from config.guard import (
    build_guard_hook_commands,
    guard_hooks_present,
    merge_guard_hooks,
)
from config.env import (
    BuildResult,
    WriteResult,
    organization_choices,
    select_organization,
    summarize_env_write,
)
from config.statusline import (
    build_statusline_command,
    detect_python3,
    install_state,
    install_user_statusline,
    installed_script,
    merge_statusline_settings,
    remove_local_statusline,
    statusline_settings,
    script_source,
    uses_installed_script,
    validate_python,
)


# ---------------------------------------------------------------------------
# Shared paths, settings writes, and command quoting
# ---------------------------------------------------------------------------


def test_local_settings_path():
    assert local_settings_path(Path("/repo")) == Path(
        "/repo/.claude/settings.local.json"
    )


def test_user_claude_dir_uses_nonempty_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-custom")
    assert user_claude_dir() == tmp_path / "home" / ".claude-custom"


@pytest.mark.parametrize("override", [None, ""])
def test_user_claude_dir_defaults_to_home(monkeypatch, tmp_path, override):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if override is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", override)
    assert user_claude_dir() == tmp_path / "home" / ".claude"


@pytest.mark.parametrize(
    ("override", "expected_suffix"),
    [
        ("~/.claude-fresh", ".claude-fresh"),
        ("", ".claude"),
        (None, ".claude"),
    ],
)
def test_fresh_import_constants_honor_claude_config_dir(
    tmp_path, override, expected_suffix
):
    home = tmp_path / "controlled-home"
    env = os.environ.copy()
    env["HOME"] = str(home)
    if override is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = override
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from config import common; "
                "print(json.dumps([str(common.USER_CLAUDE_DIR), "
                "str(common.USER_SETTINGS_PATH), "
                "str(common.LOCAL_SETTINGS_PATH)]))"
            ),
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    user_dir, user_settings, local_settings = json.loads(result.stdout)
    expected_user_dir = home / expected_suffix
    assert Path(user_dir) == expected_user_dir
    assert Path(user_settings) == expected_user_dir / "settings.json"
    assert Path(local_settings) == local_settings_path(config_common.REPO_ROOT)


def test_write_settings_creates_parent_atomically_without_temp_residue(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    write_settings(path, {"answer": 42})
    assert path.read_text(encoding="utf-8") == '{\n  "answer": 42\n}\n'
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_write_settings_replace_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    original = b'{"original":true}\r\n'
    path.write_bytes(original)

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(fsio.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        write_settings(path, {"replacement": True})
    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


# ---------------------------------------------------------------------------
# build_statusline_command / shell_quote
# ---------------------------------------------------------------------------


def test_build_command_render_posix():
    assert build_statusline_command(
        "/usr/bin/python3",
        Path("/home/u/.claude/hooks/ccstatus.py"),
    ) == "/usr/bin/python3 /home/u/.claude/hooks/ccstatus.py"


def test_build_command_posix_quotes_spaces():
    cmd = build_statusline_command(
        "/usr/bin/python3",
        Path("/my home/.claude/hooks/ccstatus.py"),
    )
    assert cmd == (
        "/usr/bin/python3 '/my home/.claude/hooks/ccstatus.py'"
    )


def test_build_command_windows_path_is_posix_quoted():
    # Claude Code runs statusLine through Git Bash on Windows: a bare
    # backslash path loses every separator there, so quote and normalize.
    cmd = build_statusline_command(
        r"C:\Program Files\Python\python.exe",
        Path(r"C:\Users\u\.claude\hooks\ccstatus.py"),
    )
    assert cmd == (
        "'C:/Program Files/Python/python.exe' "
        "C:/Users/u/.claude/hooks/ccstatus.py"
    )
    assert "\\" not in cmd


def test_build_command_windows_spaceless_path_still_survives_bash():
    # The original bug: list2cmdline left a space-free path unquoted, and
    # bash then ate every backslash.
    cmd = build_statusline_command(
        r"C:\Python\python.exe",
        Path(r"C:\Users\u\.claude\hooks\ccstatus.py"),
    )
    assert "\\" not in cmd
    assert "C:/Python/python.exe" in cmd


def test_shell_quote():
    assert shell_quote("plain") == "plain"
    assert shell_quote("has space") == "'has space'"
    assert shell_quote(r"C:\Program Files\python.exe") == "'C:/Program Files/python.exe'"
    assert shell_quote(r"C:\Python\python.exe") == "C:/Python/python.exe"


def test_posix_path_normalizes_separators():
    assert posix_path(r"C:\a\b") == "C:/a/b"
    assert posix_path("/a/b") == "/a/b"


# ---------------------------------------------------------------------------
# detect_python3
# ---------------------------------------------------------------------------


def test_detect_python3_filters_venv_entries(monkeypatch):
    monkeypatch.setattr(config_statusline.sys, "platform", "linux")
    monkeypatch.setattr(config_statusline, "validate_python", lambda *a, **k: True)
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

    monkeypatch.setattr(config_statusline.shutil, "which", fake_which)
    assert detect_python3() == "/usr/local/bin/python3"
    assert seen["name"] == "python3"
    assert ".venv" not in seen["path"]
    assert "/usr/local/bin" in seen["path"] and "/usr/bin" in seen["path"]


def test_detect_python3_posix_fallback(monkeypatch):
    monkeypatch.setattr(config_statusline.sys, "platform", "linux")
    monkeypatch.setattr(config_statusline, "validate_python", lambda *a, **k: True)
    monkeypatch.setattr(config_statusline.shutil, "which", lambda *a, **k: None)
    assert detect_python3() == "/usr/bin/python3"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_detect_python3_none_when_nothing_validates(monkeypatch, platform):
    monkeypatch.setattr(config_statusline.sys, "platform", platform)
    monkeypatch.setattr(config_statusline, "validate_python", lambda *a, **k: False)
    monkeypatch.setattr(
        config_statusline.shutil, "which", lambda *a, **k: "/somewhere/python"
    )
    assert detect_python3() is None


def test_detect_python3_win32_skips_failing_candidate(monkeypatch):
    """The Windows Store python.exe stub fails validation → next candidate."""
    monkeypatch.setattr(config_statusline.sys, "platform", "win32")
    found = {
        "python": r"C:\Store\python.exe",
        "python3": r"C:\Real\python3.exe",
        "py": None,
    }
    monkeypatch.setattr(
        config_statusline.shutil, "which", lambda name, path=None: found[name]
    )
    monkeypatch.setattr(
        config_statusline,
        "validate_python",
        lambda c, **k: c == r"C:\Real\python3.exe",
    )
    assert detect_python3() == r"C:\Real\python3.exe"


def test_detect_python3_win32_base_prefix_wins_on_empty_path(monkeypatch):
    monkeypatch.setattr(config_statusline.sys, "platform", "win32")
    monkeypatch.setattr(config_statusline.sys, "base_prefix", r"C:\uv\cpython")
    monkeypatch.setattr(config_statusline.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(config_statusline, "validate_python", lambda *a, **k: True)
    assert detect_python3() == str(Path(r"C:\uv\cpython") / "python.exe")


def test_validate_python_smoke(tmp_path):
    assert validate_python(sys.executable) is True
    assert validate_python(str(tmp_path / "does-not-exist")) is False
    assert validate_python("") is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_validate_python_rejects_nonzero_exit(tmp_path):
    bad = tmp_path / "bad-python"
    bad.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    bad.chmod(0o755)
    assert validate_python(str(bad)) is False


# ---------------------------------------------------------------------------
# install_state / install_user_statusline
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script_source(repo).write_text("# statusline v1\n", encoding="utf-8")
    return repo


def test_install_state_missing_then_current_then_outdated(tmp_path):
    repo = _fake_repo(tmp_path)
    user_dir = tmp_path / ".claude"
    assert install_state(repo, user_dir) == "missing"
    install_user_statusline(repo, user_dir, "/usr/bin/python3")
    assert install_state(repo, user_dir) == "current"
    script_source(repo).write_text("# statusline v2\n", encoding="utf-8")
    assert install_state(repo, user_dir) == "outdated"


def test_install_user_statusline_copies_and_wires(tmp_path):
    repo = _fake_repo(tmp_path)
    user_dir = tmp_path / ".claude"
    settings = user_dir / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["X"]}}), encoding="utf-8"
    )
    command = install_user_statusline(
        repo, user_dir, "/usr/bin/python3"
    )
    target = installed_script(user_dir)
    assert target.read_text(encoding="utf-8") == "# statusline v1\n"
    assert command == f"/usr/bin/python3 {posix_path(target)}"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["X"]}  # untouched
    assert data["statusLine"] == {
        "type": "command",
        "command": command,
        "padding": 0,
    }
    # reinstall is idempotent
    assert (
        install_user_statusline(repo, user_dir, "/usr/bin/python3")
        == command
    )


def test_install_user_statusline_uses_explicit_settings_path(tmp_path):
    repo = _fake_repo(tmp_path)
    user_dir = tmp_path / "user-config"
    explicit_settings = tmp_path / "separate-target" / "settings.json"

    command = install_user_statusline(
        repo,
        user_dir,
        "/usr/bin/python3",        settings_path=explicit_settings,
    )

    assert statusline_settings(explicit_settings) == command
    assert not (user_dir / "settings.json").exists()
    assert installed_script(user_dir).read_text(encoding="utf-8") == (
        "# statusline v1\n"
    )


def test_install_user_statusline_missing_source(tmp_path):
    with pytest.raises(SetupError):
        install_user_statusline(
            tmp_path / "no-repo", tmp_path / ".claude", "/usr/bin/python3"
        )


def test_install_user_statusline_never_clobbers_malformed_settings(tmp_path):
    repo = _fake_repo(tmp_path)
    user_dir = tmp_path / ".claude"
    settings = user_dir / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{broken", encoding="utf-8")
    with pytest.raises(SetupError):
        install_user_statusline(repo, user_dir, "/usr/bin/python3")
    assert settings.read_text(encoding="utf-8") == "{broken"
    assert not installed_script(user_dir).exists()


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
        ("Organization 2", ""),
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
    assert "2 added" in text
    assert "1 updated" in text
    assert "1 already saved" in text
    assert "1 skipped (empty)" in text


# ---------------------------------------------------------------------------
# statusline_settings / merge_statusline_settings
# ---------------------------------------------------------------------------


def test_statusline_settings(tmp_path):
    path = tmp_path / "settings.json"
    assert statusline_settings(path) is None
    path.write_text("{malformed", encoding="utf-8")
    assert statusline_settings(path) is None
    path.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "x", "padding": 0}}),
        encoding="utf-8",
    )
    assert statusline_settings(path) == "x"


def test_merge_statusline_settings_preserves_keys_and_skips_identical(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["X"]}, "prefersReducedMotion": False}),
        encoding="utf-8",
    )
    assert merge_statusline_settings(path, "cmd one") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["X"]}
    assert data["prefersReducedMotion"] is False
    assert data["statusLine"] == {"type": "command", "command": "cmd one", "padding": 0}
    # identical → skip
    assert merge_statusline_settings(path, "cmd one") is False
    # different → rewrite
    assert merge_statusline_settings(path, "cmd two") is True
    assert statusline_settings(path) == "cmd two"


def test_merge_statusline_settings_creates_missing_file(tmp_path):
    path = tmp_path / "sub" / "settings.json"
    assert merge_statusline_settings(path, "cmd") is True
    assert statusline_settings(path) == "cmd"


@pytest.mark.parametrize("content", ["{broken", "[1]"])
def test_merge_statusline_settings_never_clobbers_malformed(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SetupError):
        merge_statusline_settings(path, "cmd")
    assert path.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# uses_installed_script / remove_local_statusline
# ---------------------------------------------------------------------------


def test_uses_installed_script(tmp_path):
    user_dir = tmp_path / ".claude"
    # the forward-slash form we write, on either host
    script = posix_path(installed_script(user_dir))
    assert uses_installed_script(f"/usr/bin/python3 {script}", user_dir)
    # shlex-quoted paths keep the raw path as a substring
    assert uses_installed_script(f"/usr/bin/python3 '{script}'", user_dir)
    assert not uses_installed_script("npx -y ccstatusline@latest", user_dir)
    assert not uses_installed_script(
        "/usr/bin/python3 /other/home/.claude/hooks/ccstatus.py",
        user_dir,
    )
    assert not uses_installed_script(None, user_dir)
    assert not uses_installed_script("", user_dir)


def test_uses_installed_script_rejects_stale_backslash_command():
    """A pre-fix install has a cmd-quoted backslash command that cannot run in
    the POSIX shell Claude Code uses, so it must read as not-wired and prompt
    a reinstall rather than showing a green hub row over a dead status bar."""
    user_dir = Path(r"C:\Users\u\.claude")
    script = installed_script(user_dir)
    posix = str(script).replace("\\", "/")
    assert uses_installed_script(f"'C:/py/python.exe' '{posix}'", user_dir)
    assert not uses_installed_script(
        f'"C:\\py\\python.exe" "{script}"', user_dir
    )


def test_remove_local_statusline_preserves_other_keys(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["X"]},
                "statusLine": {"type": "command", "command": "cmd", "padding": 0},
            }
        ),
        encoding="utf-8",
    )
    assert remove_local_statusline(path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"permissions": {"allow": ["X"]}}
    # nothing left to remove → False
    assert remove_local_statusline(path) is False


def test_remove_local_statusline_missing_file(tmp_path):
    assert remove_local_statusline(tmp_path / "nope.json") is False


@pytest.mark.parametrize("content", ["", "   \n\t "])
def test_empty_settings_file_reads_as_no_settings(tmp_path, content):
    """A hand-repaired settings file is often left empty; that is 'nothing
    configured yet', not malformed JSON we must refuse to touch."""
    path = tmp_path / "settings.local.json"
    path.write_text(content, encoding="utf-8")
    assert merge_guard_hooks(path, "/usr/bin/python3", Path("/repo")) is True
    assert guard_hooks_present(path)


@pytest.mark.parametrize("content", ["{broken", "[1]"])
def test_remove_local_statusline_never_clobbers_malformed(tmp_path, content):
    path = tmp_path / "settings.local.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SetupError):
        remove_local_statusline(path)
    assert path.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Guard hooks (config/guard.py)
# ---------------------------------------------------------------------------


def test_build_guard_commands_posix():
    cmds = build_guard_hook_commands("/usr/bin/python3", Path("/repo"))
    assert cmds["PreToolUse"] == (
        "[ ! -d /repo/.ccguard ] || /usr/bin/python3 /repo/usage_guard.py --hook-json"
    )
    assert cmds["SessionStart"] == "/usr/bin/python3 /repo/usage_guard.py --session-start"


def test_build_guard_commands_posix_quotes_spaces():
    cmds = build_guard_hook_commands("/usr/bin/python3", Path("/my repo"))
    assert cmds["PreToolUse"] == (
        "[ ! -d '/my repo/.ccguard' ] || /usr/bin/python3 "
        "'/my repo/usage_guard.py' --hook-json"
    )
    assert cmds["SessionStart"] == (
        "/usr/bin/python3 '/my repo/usage_guard.py' --session-start"
    )


def test_build_guard_commands_windows_paths_are_posix():
    # Claude Code runs hook commands through Git Bash on Windows, never cmd:
    # `if exist` doesn't parse there, and a trailing backslash before a quote
    # escapes it, which took down every tool call in the session.
    cmds = build_guard_hook_commands(r"C:\Python\python.exe", Path(r"C:\repo"))
    assert cmds["PreToolUse"] == (
        "[ ! -d C:/repo/.ccguard ] || C:/Python/python.exe "
        "C:/repo/usage_guard.py --hook-json"
    )
    assert cmds["SessionStart"] == (
        "C:/Python/python.exe C:/repo/usage_guard.py --session-start"
    )


def test_build_guard_commands_windows_spaces():
    cmds = build_guard_hook_commands(
        r"C:\Program Files\Python\python.exe", Path(r"C:\my repo")
    )
    assert cmds["PreToolUse"] == (
        "[ ! -d 'C:/my repo/.ccguard' ] || 'C:/Program Files/Python/python.exe' "
        "'C:/my repo/usage_guard.py' --hook-json"
    )
    assert cmds["SessionStart"] == (
        "'C:/Program Files/Python/python.exe' "
        "'C:/my repo/usage_guard.py' --session-start"
    )


@pytest.mark.parametrize(
    "python3, repo",
    [
        ("/usr/bin/python3", Path("/repo")),
        ("/usr/bin/python3", Path("/my repo")),
        (r"C:\Python\python.exe", Path(r"C:\repo")),
        (r"C:\Program Files\Python\python.exe", Path(r"C:\my repo")),
    ],
)
def test_guard_commands_are_always_bash_safe(python3, repo):
    """Regression guard for the cmd-syntax bug: nothing we emit may contain a
    backslash (bash eats it) or cmd-only `if exist`."""
    for command in build_guard_hook_commands(python3, repo).values():
        assert "\\" not in command
        assert "if exist" not in command
    statusline = build_statusline_command(python3, Path(repo) / "ccstatus.py")
    assert "\\" not in statusline


def test_merge_guard_hooks_creates_file_and_is_idempotent(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    assert merge_guard_hooks(path, "/usr/bin/python3", Path("/repo")) is True
    before = path.read_text(encoding="utf-8")
    data = json.loads(before)
    assert [e["matcher"] for e in data["hooks"]["PreToolUse"]] == ["*"]
    assert [e["matcher"] for e in data["hooks"]["SessionStart"]] == ["startup|clear"]
    # second run: identical → no write
    assert merge_guard_hooks(path, "/usr/bin/python3", Path("/repo")) is False
    assert path.read_text(encoding="utf-8") == before


def test_merge_guard_hooks_replaces_handwritten_entries(tmp_path):
    """Seeded with the real hand-written settings.local.json shape: old
    /usr/bin/python3 hooks are replaced (no duplication), everything else
    stays byte-identical."""
    path = tmp_path / "settings.local.json"
    seeded = {
        "permissions": {"allow": ["Bash(uv run:*)", "Bash(git reset:*)"]},
        "prefersReducedMotion": False,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "[ ! -d /old/.ccguard ] || /usr/bin/python3 "
                                "/old/usage_guard.py --hook-json"
                            ),
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "startup|clear",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/usr/bin/python3 /old/usage_guard.py --session-start",
                        }
                    ],
                }
            ],
        },
    }
    path.write_text(json.dumps(seeded), encoding="utf-8")
    assert merge_guard_hooks(path, "/new/python3", Path("/repo")) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == seeded["permissions"]
    assert data["prefersReducedMotion"] is False
    for event in ("PreToolUse", "SessionStart"):
        entries = data["hooks"][event]
        assert len(entries) == 1
        commands = [h["command"] for h in entries[0]["hooks"]]
        assert len(commands) == 1
        assert "/new/python3" in commands[0]
        assert "/old/" not in commands[0]


def test_merge_guard_hooks_preserves_foreign_hooks_and_events(tmp_path):
    path = tmp_path / "settings.local.json"
    foreign = {"type": "command", "command": "echo hi"}
    ours_old = {
        "type": "command",
        "command": "/usr/bin/python3 /x/usage_guard.py --hook-json",
    }
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"matcher": "*", "hooks": [foreign, ours_old]}],
                    "Stop": [{"matcher": "", "hooks": [foreign]}],
                }
            }
        ),
        encoding="utf-8",
    )
    assert merge_guard_hooks(path, "/p", Path("/repo")) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [foreign]}]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "*", "hooks": [foreign]}  # foreign survives
    assert len(pre) == 2
    assert "usage_guard.py" in pre[1]["hooks"][0]["command"]


@pytest.mark.parametrize(
    "content",
    ["{broken", "[1]", '{"hooks": "nope"}', '{"hooks": {"PreToolUse": {}}}'],
)
def test_merge_guard_hooks_never_clobbers_malformed(tmp_path, content):
    path = tmp_path / "settings.local.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SetupError):
        merge_guard_hooks(path, "/p", Path("/repo"))
    assert path.read_text(encoding="utf-8") == content


def test_guard_hooks_present_truth_table(tmp_path):
    path = tmp_path / "settings.local.json"
    assert guard_hooks_present(path) is False  # missing file
    path.write_text("{broken", encoding="utf-8")
    assert guard_hooks_present(path) is False  # malformed
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert guard_hooks_present(path) is False  # no events
    only_pre = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "p usage_guard.py --hook-json"}],
                }
            ]
        }
    }
    path.write_text(json.dumps(only_pre), encoding="utf-8")
    assert guard_hooks_present(path) is False  # one event is not enough
    merge_guard_hooks(path, "/p", Path("/repo"))
    assert guard_hooks_present(path) is True


# ---------------------------------------------------------------------------
# Coordinated statusline + guard installer
# ---------------------------------------------------------------------------


def _isolate_install_globals(monkeypatch, tmp_path, *, repo_name="repo"):
    """Patch every app path global, including intentionally divergent paths."""
    repo = tmp_path / repo_name
    repo.mkdir()
    script_source(repo).write_text("# isolated statusline\n", encoding="utf-8")
    local_settings = tmp_path / "local-target" / "settings.local.json"
    user_dir = tmp_path / "user-config"
    user_settings = tmp_path / "user-target" / "settings.json"
    monkeypatch.setattr(config_app, "REPO_ROOT", repo)
    monkeypatch.setattr(config_app, "LOCAL_SETTINGS_PATH", local_settings)
    monkeypatch.setattr(config_app, "USER_CLAUDE_DIR", user_dir)
    monkeypatch.setattr(config_app, "USER_SETTINGS_PATH", user_settings)
    return repo, local_settings, user_dir, user_settings


@pytest.mark.parametrize("user_content", ["{broken", "[1]"])
def test_coordinated_installer_user_preflight_aborts_before_every_write(
    tmp_path, monkeypatch, user_content
):
    repo, local_settings, user_dir, user_settings = _isolate_install_globals(
        monkeypatch, tmp_path
    )
    user_settings.parent.mkdir(parents=True)
    user_settings.write_bytes(user_content.encode("utf-8"))
    local_settings.parent.mkdir(parents=True)
    local_original = b'{"local":"untouched"}\r\n'
    local_settings.write_bytes(local_original)

    with pytest.raises(SetupError):
        install_statusline_and_guard("/usr/bin/python3")

    assert user_settings.read_bytes() == user_content.encode("utf-8")
    assert local_settings.read_bytes() == local_original
    assert not installed_script(user_dir).exists()
    assert script_source(repo).read_text(encoding="utf-8") == "# isolated statusline\n"


@pytest.mark.parametrize(
    "local_content",
    [
        "{broken",
        "[1]",
        '{"hooks":"nope"}',
        '{"hooks":{"PreToolUse":{}}}',
        '{"hooks":{"SessionStart":{}}}',
    ],
)
def test_coordinated_installer_local_preflight_aborts_before_every_write(
    tmp_path, monkeypatch, local_content
):
    _, local_settings, user_dir, user_settings = _isolate_install_globals(
        monkeypatch, tmp_path
    )
    user_settings.parent.mkdir(parents=True)
    user_original = b'{"user":"untouched"}\r\n'
    user_settings.write_bytes(user_original)
    local_settings.parent.mkdir(parents=True)
    local_original = local_content.encode("utf-8")
    local_settings.write_bytes(local_original)

    with pytest.raises(SetupError):
        install_statusline_and_guard("/usr/bin/python3")

    assert user_settings.read_bytes() == user_original
    assert local_settings.read_bytes() == local_original
    assert not installed_script(user_dir).exists()


def test_coordinated_installer_fresh_success_returns_false(tmp_path, monkeypatch):
    repo, local_settings, user_dir, user_settings = _isolate_install_globals(
        monkeypatch, tmp_path
    )

    assert install_statusline_and_guard("/usr/bin/python3") is False

    assert installed_script(user_dir).read_bytes() == script_source(repo).read_bytes()
    assert statusline_settings(user_settings) == (
        f"/usr/bin/python3 {posix_path(installed_script(user_dir))}"
    )
    assert not (user_dir / "settings.json").exists()
    assert guard_hooks_present(local_settings)


def test_coordinated_installer_reports_removed_local_override(tmp_path, monkeypatch):
    _, local_settings, _, _ = _isolate_install_globals(monkeypatch, tmp_path)
    local_settings.parent.mkdir(parents=True)
    local_settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["X"]},
                "statusLine": {"type": "command", "command": "old"},
            }
        ),
        encoding="utf-8",
    )

    assert install_statusline_and_guard("/usr/bin/python3") is True

    data = json.loads(local_settings.read_text(encoding="utf-8"))
    assert "statusLine" not in data
    assert data["permissions"] == {"allow": ["X"]}
    assert guard_hooks_present(local_settings)


def test_coordinated_installer_writes_bash_safe_windows_commands(
    tmp_path, monkeypatch
):
    """End-to-end: a Windows interpreter path must land in both settings files
    quoted for bash, since that is the shell Claude Code runs them in."""
    _, local_settings, _, user_settings = _isolate_install_globals(
        monkeypatch, tmp_path, repo_name="repo with spaces"
    )
    install_statusline_and_guard(r"C:\Program Files\Python\python.exe")

    statusline_cmd = statusline_settings(user_settings)
    assert statusline_cmd.startswith("'C:/Program Files/Python/python.exe' ")
    assert "\\" not in statusline_cmd

    local_data = json.loads(local_settings.read_text(encoding="utf-8"))
    guard_commands = [
        hook["command"]
        for event in ("PreToolUse", "SessionStart")
        for entry in local_data["hooks"][event]
        for hook in entry["hooks"]
    ]
    assert guard_commands
    for command in guard_commands:
        assert "'C:/Program Files/Python/python.exe'" in command
        assert "\\" not in command
        assert "if exist" not in command


def test_coordinated_installer_prepares_both_files_before_copy_and_never_reloads(
    tmp_path, monkeypatch
):
    _, local_settings, _, user_settings = _isolate_install_globals(
        monkeypatch, tmp_path
    )
    local_settings.parent.mkdir(parents=True)
    local_settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["X"]},
                "statusLine": {"type": "command", "command": "old"},
            }
        ),
        encoding="utf-8",
    )
    events = []
    real_status_prepare = config_app.prepare_user_statusline
    real_guard_prepare = config_app.prepare_guard_hooks
    real_replace = config_app.replace
    real_status_load = config_statusline.load_settings_or_raise
    real_guard_load = config_guard.load_settings_or_raise
    real_copyfile = config_statusline.shutil.copyfile
    real_status_write = config_statusline.write_settings
    real_guard_write = config_guard.write_settings

    def status_prepare(*args, **kwargs):
        events.append("status-prepare-start")
        result = real_status_prepare(*args, **kwargs)
        events.append(("status-prepare-end", result.settings_changed))
        return result

    def guard_prepare(*args, **kwargs):
        events.append("guard-prepare-start")
        result = real_guard_prepare(*args, **kwargs)
        events.append("guard-prepare-end")
        return result

    def finalize_local(prepared, **changes):
        assert "statusLine" not in changes["settings"]
        assert changes["changed"] is True
        events.append(("local-finalized", changes["changed"]))
        return real_replace(prepared, **changes)

    def status_load(path):
        events.append(("settings-load", Path(path)))
        return real_status_load(path)

    def guard_load(path):
        events.append(("settings-load", Path(path)))
        return real_guard_load(path)

    def copyfile(*args, **kwargs):
        events.append("script-copy")
        return real_copyfile(*args, **kwargs)

    def status_write(path, data):
        events.append(("settings-write", Path(path)))
        return real_status_write(path, data)

    def guard_write(path, data):
        events.append(("settings-write", Path(path)))
        return real_guard_write(path, data)

    monkeypatch.setattr(config_app, "prepare_user_statusline", status_prepare)
    monkeypatch.setattr(config_app, "prepare_guard_hooks", guard_prepare)
    monkeypatch.setattr(config_app, "replace", finalize_local)
    monkeypatch.setattr(config_statusline, "load_settings_or_raise", status_load)
    monkeypatch.setattr(config_guard, "load_settings_or_raise", guard_load)
    monkeypatch.setattr(config_statusline.shutil, "copyfile", copyfile)
    monkeypatch.setattr(config_statusline, "write_settings", status_write)
    monkeypatch.setattr(config_guard, "write_settings", guard_write)

    install_statusline_and_guard("/usr/bin/python3")

    assert events == [
        "status-prepare-start",
        ("settings-load", user_settings),
        ("status-prepare-end", True),
        "guard-prepare-start",
        ("settings-load", local_settings),
        "guard-prepare-end",
        ("local-finalized", True),
        "script-copy",
        ("settings-write", user_settings),
        ("settings-write", local_settings),
    ]


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

    monkeypatch.setattr(config_app.asyncio, "create_subprocess_exec", fake_exec)
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

    monkeypatch.setattr(config_app.asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(upstream_ref(Path("/repo"))) is None


# ---------------------------------------------------------------------------
# hub_status
# ---------------------------------------------------------------------------


def _isolate_user_claude(monkeypatch, tmp_path):
    """Point hub_status at a scratch ~/.claude, not the real one."""
    user_dir = tmp_path / "home" / ".claude"
    monkeypatch.setattr(config_app, "USER_CLAUDE_DIR", user_dir)
    monkeypatch.setattr(config_app, "USER_SETTINGS_PATH", user_dir / "settings.json")
    return user_dir


def test_hub_status_rows_independent_on_malformed_settings(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    _isolate_user_claude(monkeypatch, tmp_path)
    (repo / "settings.json").write_text("{broken", encoding="utf-8")
    rows = hub_status(repo)
    assert set(rows) == {"env", "sidecar", "statusline", "ocr"}
    assert "a settings file has a problem" in rows["sidecar"]
    assert "delete settings.json and run setup again" in rows["sidecar"]
    assert rows["env"] == "not set up yet"
    assert rows["statusline"] == "not set up yet"


def test_hub_status_statusline_lifecycle(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    user_dir = _isolate_user_claude(monkeypatch, tmp_path)
    # nothing installed → not set up
    assert hub_status(repo)["statusline"] == "not set up yet"
    # installed and wired, but guard hooks not yet written → flagged
    install_user_statusline(repo, user_dir, "/usr/bin/python3")
    assert hub_status(repo)["statusline"] == (
        "needs attention — the usage guard isn't hooked up in this "
        "folder (open this item to fix)"
    )
    # guard hooks installed too → ready
    local = repo / ".claude" / "settings.local.json"
    merge_guard_hooks(local, "/usr/bin/python3", repo)
    assert hub_status(repo)["statusline"] == (
        "ready — status bar and usage guard installed, works in every folder"
    )
    # repo script updated → prompt to reinstall
    script_source(repo).write_text("# statusline v2\n", encoding="utf-8")
    assert (
        hub_status(repo)["statusline"]
        == "needs attention — an updated status bar is ready to install"
    )
    # a repo-local statusLine shadows the account-wide one → flagged first
    merge_statusline_settings(local, "anything")
    assert hub_status(repo)["statusline"] == (
        "needs attention — this folder overrides your account-wide "
        "status bar (open this item to fix)"
    )


def test_hub_status_script_copied_but_not_wired_is_not_configured(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    user_dir = _isolate_user_claude(monkeypatch, tmp_path)
    install_user_statusline(repo, user_dir, "/usr/bin/python3")
    # statusLine replaced by something that never runs the script → the cache
    # never refreshes → not configured
    merge_statusline_settings(user_dir / "settings.json", "npx -y ccstatusline@latest")
    assert hub_status(repo)["statusline"] == "not set up yet"


def test_hub_status_uses_local_settings_path_for_override_and_guard(
    tmp_path, monkeypatch
):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    user_dir = _isolate_user_claude(monkeypatch, tmp_path)
    custom_local = tmp_path / "elsewhere" / "settings.local.json"
    seen = []

    def custom_local_path(repo_root):
        seen.append(Path(repo_root))
        return custom_local

    monkeypatch.setattr(config_app, "local_settings_path", custom_local_path)
    install_user_statusline(repo, user_dir, "/usr/bin/python3")
    merge_guard_hooks(custom_local, "/usr/bin/python3", repo)
    assert hub_status(repo)["statusline"] == (
        "ready — status bar and usage guard installed, works in every folder"
    )
    merge_statusline_settings(custom_local, "custom override")
    assert hub_status(repo)["statusline"] == (
        "needs attention — this folder overrides your account-wide "
        "status bar (open this item to fix)"
    )
    assert seen == [repo, repo]


# ---------------------------------------------------------------------------
# OCR task (config/ocr.py)
# ---------------------------------------------------------------------------


def _fake_platform(monkeypatch, *, windows: bool):
    monkeypatch.setattr(config_ocr, "_is_windows", lambda: windows)


def test_find_focr_prefers_path(monkeypatch):
    _fake_platform(monkeypatch, windows=False)
    monkeypatch.setattr(config_ocr.shutil, "which", lambda name: "/usr/bin/focr")
    assert config_ocr.find_focr() == "/usr/bin/focr"


def test_find_focr_falls_back_to_installer_dir(monkeypatch, tmp_path):
    """A fresh POSIX install only edits shell rc files, so the binary is not
    on PATH in this process — it must still be found."""
    _fake_platform(monkeypatch, windows=False)
    monkeypatch.setattr(config_ocr.shutil, "which", lambda name: None)
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "focr"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(config_ocr.Path, "home", staticmethod(lambda: home))
    assert config_ocr.find_focr() == str(binary)


def test_find_focr_none_when_nowhere(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=False)
    monkeypatch.setattr(config_ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(config_ocr.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(config_ocr, "install_dirs", lambda: [tmp_path / "nope"])
    assert config_ocr.find_focr() is None


def test_windows_binary_and_dirs_use_localappdata(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert config_ocr.binary_name() == "focr.exe"
    assert config_ocr.install_dirs() == [
        tmp_path / "AppData" / "Local" / "Programs" / "focr"
    ]
    assert config_ocr.model_cache_dir() == tmp_path / "AppData" / "Local" / "franken_ocr"


def test_windows_dirs_fall_back_to_userprofile(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert config_ocr.install_dirs() == [
        tmp_path / "AppData" / "Local" / "Programs" / "focr"
    ]


def test_model_installed_finds_nested_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(config_ocr, "model_cache_dir", lambda: tmp_path)
    assert config_ocr.model_installed() is False
    nested = tmp_path / "models" / "got-ocr2"
    nested.mkdir(parents=True)
    (nested / "got-ocr2.int8.focrq").write_bytes(b"weights")
    assert config_ocr.model_installed() is True


def test_model_installed_false_without_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config_ocr, "model_cache_dir", lambda: tmp_path / "missing")
    assert config_ocr.model_installed() is False


@pytest.mark.parametrize(
    "focr,model,expected",
    [
        (None, False, config_ocr.STATE_MISSING),
        (None, True, config_ocr.STATE_MISSING),
        ("/usr/bin/focr", False, config_ocr.STATE_NO_MODEL),
        ("/usr/bin/focr", True, config_ocr.STATE_READY),
    ],
)
def test_ocr_state_matrix(monkeypatch, focr, model, expected):
    monkeypatch.setattr(config_ocr, "find_focr", lambda: focr)
    monkeypatch.setattr(config_ocr, "model_installed", lambda: model)
    assert config_ocr.ocr_state() == expected


def test_installer_command_posix(tmp_path, monkeypatch):
    _fake_platform(monkeypatch, windows=False)
    script = tmp_path / "install.sh"
    assert config_ocr.installer_url() == config_ocr.INSTALL_SH_URL
    assert config_ocr.installer_filename() == "install.sh"
    # --no-pull: the model download is our own next step, not the installer's.
    assert config_ocr.installer_command(script) == [
        "bash",
        str(script),
        "--easy-mode",
        "--no-pull",
        "--no-gum",
    ]


def test_installer_command_windows(tmp_path, monkeypatch):
    _fake_platform(monkeypatch, windows=True)
    script = tmp_path / "install.ps1"
    assert config_ocr.installer_url() == config_ocr.INSTALL_PS1_URL
    assert config_ocr.installer_filename() == "install.ps1"
    assert config_ocr.installer_command(script) == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-NoPull",
    ]


def test_pull_command_uses_resolved_binary():
    assert config_ocr.pull_command("/opt/focr") == ["/opt/focr", "pull"]


def test_download_installer_writes_script(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=False)

    class Response:
        content = b"#!/usr/bin/env bash\necho hi\n"

        def raise_for_status(self):
            return None

    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr(config_ocr.httpx, "get", fake_get)
    path = config_ocr.download_installer(tmp_path)
    assert seen["url"] == config_ocr.INSTALL_SH_URL
    assert path == tmp_path / "install.sh"
    assert path.read_bytes() == Response.content


def test_download_installer_network_failure_is_setup_error(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=False)

    def boom(url, **kwargs):
        raise config_ocr.httpx.ConnectError("offline")

    monkeypatch.setattr(config_ocr.httpx, "get", boom)
    with pytest.raises(SetupError) as excinfo:
        config_ocr.download_installer(tmp_path)
    assert "internet connection" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_status_text_states(monkeypatch):
    monkeypatch.setattr(config_ocr.shutil, "which", lambda name: "/usr/bin/focr")
    assert "not set up yet" in config_ocr.status_text(config_ocr.STATE_MISSING, None)
    assert "model hasn't been downloaded" in config_ocr.status_text(
        config_ocr.STATE_NO_MODEL, "/usr/bin/focr"
    )
    assert config_ocr.status_text(config_ocr.STATE_READY, "/usr/bin/focr").startswith(
        "ready"
    )
    # installed, but not yet on PATH in this process → say so
    monkeypatch.setattr(config_ocr.shutil, "which", lambda name: None)
    assert "restart your terminal" in config_ocr.status_text(
        config_ocr.STATE_READY, "/home/u/.local/bin/focr"
    )


def test_hub_status_ocr_row(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    _isolate_user_claude(monkeypatch, tmp_path)
    monkeypatch.setattr(config_app, "ocr_state", lambda: config_ocr.STATE_MISSING)
    monkeypatch.setattr(config_app, "find_focr", lambda: None)
    assert hub_status(repo)["ocr"] == "not set up yet — scanned PDFs can't be read yet"


def test_hub_status_ocr_row_survives_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", repo / "settings.json")
    _isolate_user_claude(monkeypatch, tmp_path)

    def boom():
        raise OSError("no home directory")

    monkeypatch.setattr(config_app, "ocr_state", boom)
    rows = hub_status(repo)
    assert "couldn't check this item" in rows["ocr"]
    assert rows["env"] == "not set up yet"  # other rows unaffected
