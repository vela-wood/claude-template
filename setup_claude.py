#!/usr/bin/env python3
"""Setup hub for this repo: a Textual TUI (human-only — agents must never
run it) with three independent tasks:

1. Caption credentials → .env
2. Sidecar naming preference → repo-root settings.json (repo_settings.py)
3. Statusline + usage cache → .claude/settings.local.json

Run with `uv run setup_claude.py`. TUI-only; no plain fallback.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx
from dotenv.parser import parse_stream

import repo_settings
from repo_settings import RepoSettingsError

SETUP_PAGE_URL = "https://app.caption.fyi/claude_setup"
SETUP_API_URL = "https://chat.caption.fyi/claude_setup"
ROOT_KEYS_TO_SKIP = {"primary_email_address", "organizations"}
NAMED_CREDENTIAL_KEY_FIELDS = ("name", "key", "env", "env_var", "variable")
NAMED_CREDENTIAL_VALUE_FIELDS = ("value", "token", "secret", "credential", "url", "api_key")

REPO_ROOT = Path(__file__).resolve().parent
ENV_FILE = REPO_ROOT / ".env"
LOCAL_SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.local.json"
# Pinned at implementation time (2026-08); upgrading is a one-line change
# here — never a per-refresh `@latest` resolution in the statusline command.
PINNED_CCSTATUSLINE_VERSION = "2.2.27"

TASK_ORDER = ("env", "sidecar", "statusline")
TASK_LABELS = {
    "env": "Caption credentials",
    "sidecar": "Sidecar naming",
    "statusline": "Statusline + usage cache",
}


@dataclass(frozen=True)
class BuildResult:
    env_values: dict[str, str]
    skipped_null_keys: tuple[str, ...]


@dataclass(frozen=True)
class WriteResult:
    appended_new_keys: tuple[str, ...]
    appended_conflicting_keys: tuple[str, ...]
    skipped_existing_keys: tuple[str, ...]


class SetupError(Exception):
    pass


def _clean_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned


def drop_nulls(value: object) -> object:
    if isinstance(value, Mapping):
        cleaned_mapping = {
            key: drop_nulls(child_value)
            for key, child_value in value.items()
            if child_value is not None
        }
        if (
            any(field in cleaned_mapping for field in NAMED_CREDENTIAL_KEY_FIELDS)
            and not any(field in cleaned_mapping for field in NAMED_CREDENTIAL_VALUE_FIELDS)
        ):
            return None
        return cleaned_mapping
    if isinstance(value, list):
        cleaned_items: list[object] = []
        for item in value:
            if item is None:
                continue
            cleaned_item = drop_nulls(item)
            if cleaned_item is None:
                continue
            cleaned_items.append(cleaned_item)
        return cleaned_items
    return value


def normalize_env_key(raw_key: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", raw_key).strip("_").upper()
    if not normalized:
        raise SetupError(f"Cannot convert {raw_key!r} into an environment variable name.")
    if normalized[0].isdigit():
        normalized = f"KEY_{normalized}"
    return normalized


def stringify_scalar(value: object, *, source: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise SetupError(f"Unsupported non-scalar value at {source}.")


def merge_env_value(env_values: dict[str, str], key: str, value: object, *, source: str) -> None:
    string_value = stringify_scalar(value, source=source)
    existing_value = env_values.get(key)
    if existing_value is not None and existing_value != string_value:
        raise SetupError(
            f"Conflicting values for {key}: {existing_value!r} from earlier data and {string_value!r} from {source}."
        )
    env_values[key] = string_value


def collect_prefixed_values(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    key_prefix: str,
    value: object,
    *,
    source: str,
) -> None:
    if value is None:
        skipped_null_keys.add(key_prefix)
        return

    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            child_prefix = f"{key_prefix}_{normalize_env_key(str(child_key))}"
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                child_prefix,
                child_value,
                source=f"{source}.{child_key}",
            )
        return

    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            child_prefix = f"{key_prefix}_{index}"
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                child_prefix,
                item,
                source=f"{source}[{index}]",
            )
        return

    merge_env_value(env_values, key_prefix, value, source=source)


def extract_named_credential(item: Mapping[str, object]) -> tuple[str, object] | None:
    raw_name: object | None = None
    for field in NAMED_CREDENTIAL_KEY_FIELDS:
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate.strip():
            raw_name = candidate
            break

    if raw_name is None:
        return None

    for field in NAMED_CREDENTIAL_VALUE_FIELDS:
        if field in item:
            return normalize_env_key(raw_name), item[field]

    raise SetupError(f"Credential entry for {raw_name!r} is missing a value field.")


def collect_organization_credentials(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    credentials: object,
    *,
    source: str,
) -> None:
    if credentials is None:
        return

    if isinstance(credentials, Mapping):
        for key, value in credentials.items():
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                normalize_env_key(str(key)),
                value,
                source=f"{source}.{key}",
            )
        return

    if isinstance(credentials, list):
        for index, item in enumerate(credentials, start=1):
            item_source = f"{source}[{index}]"
            if item is None:
                continue
            if not isinstance(item, Mapping):
                raise SetupError(f"Unsupported credential entry at {item_source}.")

            named_credential = extract_named_credential(item)
            if named_credential is not None:
                key, value = named_credential
                if value is None:
                    skipped_null_keys.add(key)
                    continue
                if isinstance(value, Mapping) or isinstance(value, list):
                    collect_prefixed_values(env_values, skipped_null_keys, key, value, source=item_source)
                    continue
                merge_env_value(env_values, key, value, source=item_source)
                continue

            for key, value in item.items():
                collect_prefixed_values(
                    env_values,
                    skipped_null_keys,
                    normalize_env_key(str(key)),
                    value,
                    source=f"{item_source}.{key}",
                )
        return

    raise SetupError(f"Unsupported credentials payload at {source}.")


def collect_organization_metadata(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    organization: Mapping[str, object],
    *,
    source: str,
) -> None:
    organization_id = organization.get("organization_id")
    if organization_id is None:
        skipped_null_keys.add("ORGANIZATION_ID")
        return

    merge_env_value(
        env_values,
        "ORGANIZATION_ID",
        organization_id,
        source=f"{source}.organization_id",
    )


def organization_choices(payload: Mapping[str, object]) -> list[tuple[str, str]]:
    """Validated [(organization_name, organization_id), ...] from a payload."""
    organizations = payload.get("organizations", [])
    if organizations is None:
        return []
    if not isinstance(organizations, list):
        raise SetupError("'organizations' must be an array.")
    choices: list[tuple[str, str]] = []
    for index, organization in enumerate(organizations, start=1):
        if not isinstance(organization, Mapping):
            raise SetupError(f"'organizations[{index}]' must be an object.")
        organization_name = _clean_optional_text(organization.get("organization_name")) or f"Organization {index}"
        organization_id = _clean_optional_text(organization.get("organization_id")) or "<missing organization_id>"
        choices.append((organization_name, organization_id))
    return choices


def select_organization(payload: Mapping[str, object], index: int) -> Mapping[str, object]:
    """Return payload narrowed to organizations[index] (0-based)."""
    organizations = payload.get("organizations", [])
    if not isinstance(organizations, list) or not 0 <= index < len(organizations):
        raise SetupError(f"No organization at index {index}.")
    selected_payload = dict(payload)
    selected_payload["organizations"] = [organizations[index]]
    return selected_payload


def build_env_values(payload: Mapping[str, object]) -> BuildResult:
    env_values: dict[str, str] = {}
    skipped_null_keys: set[str] = set()

    organizations = payload.get("organizations", [])
    if organizations is None:
        organizations = []
    if not isinstance(organizations, list):
        raise SetupError("'organizations' must be an array.")

    for key, value in payload.items():
        if key in ROOT_KEYS_TO_SKIP:
            continue
        collect_prefixed_values(
            env_values,
            skipped_null_keys,
            normalize_env_key(key),
            value,
            source=f"payload.{key}",
        )

    for index, organization in enumerate(organizations, start=1):
        if not isinstance(organization, Mapping):
            raise SetupError(f"'organizations[{index}]' must be an object.")
        collect_organization_metadata(
            env_values,
            skipped_null_keys,
            organization,
            source=f"organizations[{index}]",
        )
        collect_organization_credentials(
            env_values,
            skipped_null_keys,
            organization.get("credentials"),
            source=f"organizations[{index}].credentials",
        )

    return BuildResult(
        env_values=env_values,
        skipped_null_keys=tuple(sorted(skipped_null_keys)),
    )


def _payload_from_response(response: httpx.Response) -> Mapping[str, object]:
    if response.status_code >= 400:
        detail = response.text.strip() or response.reason_phrase
        raise SetupError(f"Setup request failed ({response.status_code}): {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SetupError("Setup request returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise SetupError("Setup request returned non-object JSON.")

    cleaned_payload = drop_nulls(payload)
    if not isinstance(cleaned_payload, dict):
        raise SetupError("Setup request returned invalid object JSON.")
    return cleaned_payload


def fetch_setup_payload(auth_token: str) -> Mapping[str, object]:
    headers = {"Authorization": f"Bearer {auth_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(SETUP_API_URL, headers=headers)
    return _payload_from_response(response)


async def fetch_setup_payload_async(auth_token: str) -> Mapping[str, object]:
    """Async twin of fetch_setup_payload for @work async workers (textual
    cancels them for real; thread workers would wait out the timeout)."""
    headers = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(SETUP_API_URL, headers=headers)
    return _payload_from_response(response)


def read_existing_env_values(env_file: Path) -> dict[str, list[str]]:
    if not env_file.exists():
        return {}

    values_by_key: dict[str, list[str]] = {}
    with env_file.open(encoding="utf-8") as source:
        for binding in parse_stream(source):
            if binding.key is None or binding.value is None:
                continue
            values_by_key.setdefault(binding.key, []).append(binding.value)
    return values_by_key


def render_env_line(key: str, value: str) -> str:
    quote = not value.isalnum()
    rendered_value = "'" + value.replace("'", "\\'") + "'" if quote else value
    return f"{key}={rendered_value}\n"


def write_env_file(env_file: Path, new_values: Mapping[str, str]) -> WriteResult:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)

    existing_values = read_existing_env_values(env_file)
    appended_new_keys: list[str] = []
    appended_conflicting_keys: list[str] = []
    skipped_existing_keys: list[str] = []
    lines_to_append: list[str] = []

    for key in sorted(new_values):
        new_value = new_values[key]
        current_values = existing_values.get(key, [])

        if not current_values:
            lines_to_append.append(render_env_line(key, new_value))
            appended_new_keys.append(key)
            continue

        if new_value in current_values:
            skipped_existing_keys.append(key)
            continue

        lines_to_append.append(render_env_line(key, new_value))
        appended_conflicting_keys.append(key)

    if lines_to_append:
        needs_newline = env_file.stat().st_size > 0 and not env_file.read_text(encoding="utf-8").endswith("\n")
        with env_file.open("a", encoding="utf-8") as destination:
            if needs_newline:
                destination.write("\n")
            destination.writelines(lines_to_append)

    return WriteResult(
        appended_new_keys=tuple(appended_new_keys),
        appended_conflicting_keys=tuple(appended_conflicting_keys),
        skipped_existing_keys=tuple(skipped_existing_keys),
    )


def summarize_env_write(build_result: BuildResult, write_result: WriteResult) -> str:
    """One-line toast for the credentials task."""
    parts = [f"{len(write_result.appended_new_keys)} new key(s)"]
    if write_result.appended_conflicting_keys:
        parts.append(f"{len(write_result.appended_conflicting_keys)} conflicting value(s) appended")
    if write_result.skipped_existing_keys:
        parts.append(f"{len(write_result.skipped_existing_keys)} already set")
    if build_result.skipped_null_keys:
        parts.append(f"{len(build_result.skipped_null_keys)} null value(s) skipped")
    return f"Wrote {ENV_FILE.name}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Statusline helpers (pure; §A.2)
# ---------------------------------------------------------------------------


def _is_venv_path_entry(entry: str, virtual_env: str | None) -> bool:
    if virtual_env and (entry == virtual_env or entry.startswith(virtual_env.rstrip(os.sep) + os.sep)):
        return True
    return ".venv" in Path(entry).parts


def detect_python3() -> str:
    """A python3 that outlives this venv, for the statusline command.

    Under `uv run`, both sys.executable and a naive which() resolve into
    .venv/bin — a venv that may not exist tomorrow — so PATH entries under
    $VIRTUAL_ENV (and any .venv path segment) are filtered out first.
    Fallback: /usr/bin/python3 on POSIX; on Windows whatever filtered
    which() finds (python, then the py launcher).
    """
    virtual_env = os.environ.get("VIRTUAL_ENV")
    entries = [
        entry
        for entry in (os.environ.get("PATH") or "").split(os.pathsep)
        if entry and not _is_venv_path_entry(entry, virtual_env)
    ]
    filtered_path = os.pathsep.join(entries)
    if sys.platform == "win32":
        for name in ("python", "python3", "py"):
            found = shutil.which(name, path=filtered_path)
            if found:
                return found
        return "python"
    return shutil.which("python3", path=filtered_path) or "/usr/bin/python3"


def _quote_for_platform(arg: str, platform: str) -> str:
    """shlex.quote on POSIX; list2cmdline-style double-quoting on Windows.
    Testable on any OS."""
    if platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def detect_renderer(repo_root: Path) -> str | None:
    """Pinned local ccstatusline executable path, or None."""
    bin_dir = Path(repo_root) / ".statusline" / "node_modules" / ".bin"
    candidates = ["ccstatusline.cmd", "ccstatusline"] if sys.platform == "win32" else ["ccstatusline"]
    for name in candidates:
        exe = bin_dir / name
        if exe.exists():
            return str(exe)
    return None


def build_statusline_command(
    python3: str,
    repo_root: Path,
    renderer: str | None,
    *,
    platform: str = sys.platform,
) -> str:
    """The statusLine command for .claude/settings.local.json.

    renderer=None → tee-only `--render` (zero Node); renderer=<path> → tee
    piped into the pinned local install. Never `npx -y ...@latest`.
    """
    tee = str(Path(repo_root) / ".claude" / "hooks" / "ccstatus-tee.py")
    quoted_python = _quote_for_platform(python3, platform)
    quoted_tee = _quote_for_platform(tee, platform)
    if renderer is None:
        return f"{quoted_python} {quoted_tee} --render"
    return f"{quoted_python} {quoted_tee} | {_quote_for_platform(renderer, platform)}"


def statusline_settings(path: Path) -> str | None:
    """Current statusLine command in a Claude settings file, or None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    status_line = data.get("statusLine")
    if isinstance(status_line, dict) and isinstance(status_line.get("command"), str):
        return status_line["command"]
    return None


def merge_local_settings(path: Path, statusline_cmd: str) -> bool:
    """Set statusLine in settings.local.json, preserving every other key
    (permissions, prefersReducedMotion, ...). Returns False when already
    identical (skip). Malformed JSON → SetupError; never clobber."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SetupError(f"{path} is not valid JSON ({exc}); fix or delete it first.")
        if not isinstance(data, dict):
            raise SetupError(f"{path} does not contain a JSON object; fix or delete it first.")
    else:
        data = {}
    entry = {"type": "command", "command": statusline_cmd, "padding": 0}
    if data.get("statusLine") == entry:
        return False
    data["statusLine"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Remote update check (§B.5 — notice-only, never auto-pulls, setup only)
# ---------------------------------------------------------------------------


async def _run_git(repo_root: Path, args: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_root),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
    return proc.returncode, stdout.decode(errors="replace").strip()


async def upstream_ref(repo_root: Path) -> tuple[str, str] | None:
    """(remote, branch) of @{upstream}, or None (no upstream / detached /
    no git). Local only — no fetch is ever attempted from here."""
    try:
        code, out = await _run_git(
            repo_root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            5,
        )
    except Exception:
        return None
    if code != 0 or "/" not in out:
        return None
    remote, _, branch = out.partition("/")
    return remote, branch


async def count_commits_behind(repo_root: Path) -> int | None:
    """Commits behind @{upstream} after fetching exactly that branch, or
    None — silently — on any failure. Setup works offline."""
    ref = await upstream_ref(repo_root)
    if ref is None:
        return None
    remote, branch = ref
    try:
        code, _ = await _run_git(repo_root, ["fetch", "--quiet", remote, branch], 10)
        if code != 0:
            return None
        code, out = await _run_git(
            repo_root, ["rev-list", "--count", "HEAD..@{upstream}"], 5
        )
        if code != 0:
            return None
        return int(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hub status rows
# ---------------------------------------------------------------------------


def hub_status(repo_root: Path) -> dict[str, str]:
    """One status string per task; a per-row exception becomes that row's
    text so the other rows keep working."""
    rows: dict[str, str] = {}
    try:
        values = read_existing_env_values(Path(repo_root) / ".env")
        rows["env"] = (
            f"configured ({len(values)} key(s) in .env)" if values else "not configured"
        )
    except Exception as exc:
        rows["env"] = f".env unreadable: {exc}"
    try:
        rows["sidecar"] = (
            "dotfile style (.contract.docx.md)"
            if repo_settings.read_sidecar_dotfiles()
            else "visible style (contract.docx.md)"
        )
    except Exception as exc:
        rows["sidecar"] = f"settings.json invalid: {exc} — fix or delete it"
    try:
        current = statusline_settings(Path(repo_root) / ".claude" / "settings.local.json")
        recommended = build_statusline_command(
            detect_python3(), Path(repo_root), detect_renderer(Path(repo_root))
        )
        if current is None:
            rows["statusline"] = "not configured"
        elif current == recommended:
            rows["statusline"] = "configured"
        else:
            rows["statusline"] = "differs from recommended"
    except Exception as exc:
        rows["statusline"] = f"error: {exc}"
    return rows


# ---------------------------------------------------------------------------
# Textual TUI (human-only; agents must never run this)
# ---------------------------------------------------------------------------

from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.screen import Screen  # noqa: E402
from textual.widgets import (  # noqa: E402
    Button,
    Input,
    LoadingIndicator,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option  # noqa: E402


class _DismissOnce:
    """Every button and binding path ends in dismiss(...) exactly once."""

    _finished = False

    def finish(self, result) -> None:
        if not self._finished:
            self._finished = True
            self.dismiss(result)


class HubScreen(_DismissOnce, Screen[str]):
    """Result: task id ("env" | "sidecar" | "statusline") | "all" | "done"."""

    CSS = """
    HubScreen { align: center middle; }
    #hub { width: 90; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #banner { color: $warning; margin-bottom: 1; }
    #rows { height: auto; margin-bottom: 1; }
    #buttons { height: auto; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("escape", "done", "Done")]

    def __init__(self, rows: dict[str, str], update_notice: str) -> None:
        super().__init__()
        self._rows = rows
        self._update_notice = update_notice

    def compose(self) -> ComposeResult:
        with Vertical(id="hub"):
            banner = Static(self._update_notice, id="banner")
            banner.display = bool(self._update_notice)
            yield banner
            yield Static("Select a task (Enter), or run everything:")
            yield OptionList(
                *(
                    Option(f"{TASK_LABELS[key]} — {self._rows.get(key, '?')}", id=key)
                    for key in TASK_ORDER
                ),
                id="rows",
            )
            with Horizontal(id="buttons"):
                yield Button("Run everything", id="all", variant="primary")
                yield Button("Done", id="done")

    def on_mount(self) -> None:
        self.query_one("#all", Button).focus()

    def show_banner(self, notice: str) -> None:
        banner = self.query_one("#banner", Static)
        banner.update(notice)
        banner.display = bool(notice)

    @on(OptionList.OptionSelected)
    def _row_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.finish(event.option_id)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        self.finish(event.button.id or "done")

    def action_done(self) -> None:
        self.finish("done")


class TokenScreen(_DismissOnce, Screen["Mapping[str, object] | None"]):
    """Masked token entry + async fetch. Result: payload dict or None."""

    CSS = """
    TokenScreen { align: center middle; }
    #box { width: 90; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #error { color: $error; }
    #loading { height: 1; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(
                f"Open {SETUP_PAGE_URL} in your browser (shift/cmd + click) "
                "and paste in the authentication token:"
            )
            yield Input(password=True, placeholder="Authentication token", id="token")
            loading = LoadingIndicator(id="loading")
            loading.display = False
            yield loading
            yield Static("", id="error")
            with Horizontal():
                yield Button("Submit", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#loading", LoadingIndicator).display = busy
        self.query_one("#submit", Button).disabled = busy

    def _show_error(self, message: str) -> None:
        self.query_one("#error", Static).update(message)

    @work(exclusive=True)
    async def _fetch(self, token: str) -> None:
        try:
            payload = await fetch_setup_payload_async(token)
        except (SetupError, httpx.HTTPError) as exc:
            if self.is_attached:
                self._set_busy(False)
                self._show_error(f"{exc} — fix the token and retry.")
            return
        if self.is_attached and not self._finished:
            self.finish(payload)

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        if not token:
            self._show_error("No authentication token provided.")
            return
        self._show_error("")
        self._set_busy(True)
        self._fetch(token)

    @on(Input.Submitted)
    def _input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed, "#submit")
    def _submit_pressed(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


class OrgScreen(_DismissOnce, Screen["int | None"]):
    """RadioSet of organizations; pushed only when >1. Result: 0-based index."""

    CSS = """
    OrgScreen { align: center middle; }
    #box { width: 80; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #error { color: $error; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Multiple organizations found. Select which organization's credentials to load:")
            yield RadioSet(
                *(RadioButton(f"{name}: {org_id}") for name, org_id in self._choices),
                id="orgs",
            )
            yield Static("", id="error")
            with Horizontal():
                yield Button("Select", id="select", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#select")
    def _select(self) -> None:
        index = self.query_one("#orgs", RadioSet).pressed_index
        if index < 0:  # -1 when nothing selected
            self.query_one("#error", Static).update("Select an organization first.")
            return
        self.finish(index)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


class SidecarScreen(_DismissOnce, Screen["bool | None"]):
    """Result: sidecar_dotfiles value, or None on cancel."""

    CSS = """
    SidecarScreen { align: center middle; }
    #box { width: 80; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current: bool | None) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Converted-markdown sidecar naming style:")
            yield RadioSet(
                RadioButton(
                    "contract.docx.md (visible, default)",
                    value=self._current is not True,
                ),
                RadioButton(
                    ".contract.docx.md (dot-prefixed, hidden in Finder/ls)",
                    value=self._current is True,
                ),
                id="style",
            )
            yield Static(
                "The next `uv run startup.py` migrates existing sidecars to "
                "the selected style."
            )
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        index = self.query_one("#style", RadioSet).pressed_index
        if index < 0:
            index = 0
        self.finish(index == 1)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


class StatusLineScreen(_DismissOnce, Screen["str | None"]):
    """Python-path input + live command preview. Result: confirmed command."""

    CSS = """
    StatusLineScreen { align: center middle; }
    #box { width: 100; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #preview { color: $success; margin: 1 0; }
    #note { color: $text-muted; margin-bottom: 1; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, python3: str, renderer: str | None) -> None:
        super().__init__()
        self._python3 = python3
        self._renderer = renderer

    def _command(self) -> str:
        python3 = self.query_one("#python", Input).value.strip() or self._python3
        return build_statusline_command(python3, REPO_ROOT, self._renderer)

    def _note_text(self) -> str:
        if self._renderer:
            return (
                f"Renderer: pinned ccstatusline at {self._renderer} "
                "(tee pipes into it)."
            )
        note = (
            "Renderer: none — the tee's --render mode prints a plain "
            "statusline (no Node required)."
        )
        if shutil.which("npm"):
            note += (
                " Optional: install a pinned ccstatusline "
                f"({PINNED_CCSTATUSLINE_VERSION}) with the button below."
            )
        return note

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Statusline + usage cache → .claude/settings.local.json")
            yield Static("python3 for the statusline command:")
            yield Input(value=self._python3, id="python")
            yield Static(self._command(), id="preview")
            yield Static(self._note_text(), id="note")
            with Horizontal():
                yield Button("Apply", id="apply", variant="primary")
                if self._renderer is None and shutil.which("npm"):
                    yield Button("Install ccstatusline", id="install")
                yield Button("Cancel", id="cancel")

    @on(Input.Changed, "#python")
    def _refresh_preview(self) -> None:
        self.query_one("#preview", Static).update(self._command())

    @work(exclusive=True)
    async def _install(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "install",
            "--prefix",
            str(REPO_ROOT / ".statusline"),
            f"ccstatusline@{PINNED_CCSTATUSLINE_VERSION}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), 180)
        finally:
            if proc.returncode is None:
                proc.kill()
        if not self.is_attached:
            return
        if proc.returncode == 0:
            self._renderer = detect_renderer(REPO_ROOT)
            self.query_one("#note", Static).update(self._note_text())
            self.query_one("#preview", Static).update(self._command())
            try:
                self.query_one("#install", Button).remove()
            except Exception:
                pass
        else:
            detail = (stderr or b"").decode(errors="replace").strip().splitlines()
            self.query_one("#note", Static).update(
                "npm install failed: " + (detail[-1] if detail else "unknown error")
            )

    @on(Button.Pressed, "#install")
    def _install_pressed(self) -> None:
        self.query_one("#note", Static).update("Installing ccstatusline…")
        self._install()

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        self.finish(self._command())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


class SetupApp(App[int]):
    """Hub app: one @work async driver loops hub → task → refreshed hub."""

    TITLE = "claude-template setup"

    def __init__(self) -> None:
        super().__init__()
        self.exit_code = 0
        self.update_notice = ""

    def on_mount(self) -> None:
        self._run_hub()

    @work
    async def _run_hub(self) -> None:
        try:
            self._check_updates()
            while True:
                choice = await self.push_screen_wait(
                    HubScreen(hub_status(REPO_ROOT), self.update_notice)
                )
                if choice == "done":
                    break
                for task in TASK_ORDER if choice == "all" else (choice,):
                    try:
                        await self._run_task(task)
                    except (SetupError, RepoSettingsError, OSError, httpx.HTTPError) as exc:
                        self.notify(f"{TASK_LABELS[task]} failed: {exc}", severity="error")
                        self.exit_code = 1
        except Exception as exc:  # a driver crash must still exit nonzero
            self.exit_code = 1
            try:
                self.notify(f"setup crashed: {exc}", severity="error")
            except Exception:
                pass
        self.exit(self.exit_code)

    @work(exclusive=True, group="updates")
    async def _check_updates(self) -> None:
        """Fire-and-forget; never delays the UI. Positive result unhides
        the hub banner."""
        behind = await count_commits_behind(REPO_ROOT)
        if not behind:
            return
        self.update_notice = (
            f"A newer version of this toolkit is available ({behind} update(s) "
            f"behind). To update: quit setup (Done), run "
            f"`git -C {REPO_ROOT} pull --ff-only`, then relaunch "
            "`uv run setup_claude.py`."
        )
        screen = self.screen
        if isinstance(screen, HubScreen):
            screen.show_banner(self.update_notice)

    async def _run_task(self, task: str) -> None:
        if task == "env":
            await self._task_env()
        elif task == "sidecar":
            await self._task_sidecar()
        elif task == "statusline":
            await self._task_statusline()

    async def _task_env(self) -> None:
        payload = await self.push_screen_wait(TokenScreen())
        if payload is None:
            return
        choices = organization_choices(payload)
        selected = payload
        if len(choices) > 1:  # single org → OrgScreen never pushed
            index = await self.push_screen_wait(OrgScreen(choices))
            if index is None:
                return
            selected = select_organization(payload, index)
        build_result = build_env_values(selected)
        write_result = write_env_file(ENV_FILE, build_result.env_values)
        self.notify(summarize_env_write(build_result, write_result))

    async def _task_sidecar(self) -> None:
        try:
            current: bool | None = repo_settings.read_sidecar_dotfiles()
        except RepoSettingsError:
            current = None  # screen still opens; the save will surface the error
        value = await self.push_screen_wait(SidecarScreen(current))
        if value is None:
            return
        repo_settings.update_json_object({"sidecar_dotfiles": value})
        self.notify(
            "Sidecar naming saved. The next `uv run startup.py` migrates "
            "existing sidecars."
        )

    async def _task_statusline(self) -> None:
        python3 = detect_python3()
        renderer = detect_renderer(REPO_ROOT)
        recommended = build_statusline_command(python3, REPO_ROOT, renderer)
        if statusline_settings(LOCAL_SETTINGS_PATH) == recommended:
            self.notify("Statusline already configured.")
            return
        command = await self.push_screen_wait(StatusLineScreen(python3, renderer))
        if command is None:
            return
        if merge_local_settings(LOCAL_SETTINGS_PATH, command):
            self.notify(f"Statusline written to {LOCAL_SETTINGS_PATH}.")
        else:
            self.notify("Statusline already configured.")


def main() -> int:
    result = SetupApp().run()
    return result if isinstance(result, int) else 1  # ctrl+q/crash → None → 1


if __name__ == "__main__":
    raise SystemExit(main())
