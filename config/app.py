"""Setup hub for this repo: a Textual TUI (human-only — agents must never
run it) with three independent tasks:

1. Caption credentials → .env (config/env.py)
2. Sidecar naming preference → repo-root settings.json (repo_settings.py)
3. Statusline + usage cache → .claude/settings.local.json (config/statusline.py).
   Respects a global layout: when the user-level ~/.claude/settings.json
   statusline already runs this repo's tee, nothing is written locally
   (a local statusLine would shadow it inside this repo).

Run with `uv run config.py` (a thin launcher for this module).
TUI-only; no plain fallback.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import httpx

import repo_settings
from repo_settings import RepoSettingsError

from .common import (
    ENV_FILE,
    LOCAL_SETTINGS_PATH,
    REPO_ROOT,
    USER_SETTINGS_PATH,
    SetupError,
)
from .env import (
    SETUP_PAGE_URL,
    build_env_values,
    fetch_setup_payload_async,
    organization_choices,
    read_existing_env_values,
    select_organization,
    summarize_env_write,
    write_env_file,
)
from .statusline import (
    PINNED_CCSTATUSLINE_VERSION,
    build_statusline_command,
    detect_python3,
    detect_renderer,
    merge_local_settings,
    remove_local_statusline,
    statusline_settings,
    uses_repo_tee,
)

# StatusLineScreen result meaning "remove the local statusLine and let the
# global (~/.claude/settings.json) tee'd statusline apply here". A real
# command always contains the tee path, so it can never collide.
USE_GLOBAL = "__use-global__"

TASK_ORDER = ("env", "sidecar", "statusline")
TASK_LABELS = {
    "env": "Caption credentials",
    "sidecar": "Sidecar naming",
    "statusline": "Statusline + usage cache",
}


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
        global_tee = uses_repo_tee(
            statusline_settings(USER_SETTINGS_PATH), Path(repo_root)
        )
        recommended = build_statusline_command(
            detect_python3(), Path(repo_root), detect_renderer(Path(repo_root))
        )
        if current is None:
            rows["statusline"] = (
                "configured globally (~/.claude/settings.json runs the tee)"
                if global_tee
                else "not configured"
            )
        elif global_tee:
            rows["statusline"] = (
                "local statusLine overrides the global tee'd statusline"
            )
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
from textual.screen import Screen, ScreenResultType  # noqa: E402
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


class TaskScreen(_DismissOnce, Screen[ScreenResultType]):
    """Base for the task screens: escape and the #cancel button both
    finish with None. (Must subclass Screen — textual only collects
    BINDINGS from DOMNode subclasses and @on handlers via its metaclass.)"""

    BINDINGS = [("escape", "cancel", "Cancel")]

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


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


class TokenScreen(TaskScreen["Mapping[str, object] | None"]):
    """Masked token entry + async fetch. Result: payload dict or None."""

    CSS = """
    TokenScreen { align: center middle; }
    #box { width: 90; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #error { color: $error; }
    #loading { height: 1; }
    Button { margin-right: 2; }
    """

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


class OrgScreen(TaskScreen["int | None"]):
    """RadioSet of organizations; pushed only when >1. Result: 0-based index."""

    CSS = """
    OrgScreen { align: center middle; }
    #box { width: 80; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #error { color: $error; }
    Button { margin-right: 2; }
    """

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


class SidecarScreen(TaskScreen["bool | None"]):
    """Result: sidecar_dotfiles value, or None on cancel."""

    CSS = """
    SidecarScreen { align: center middle; }
    #box { width: 80; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    Button { margin-right: 2; }
    """

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


class StatusLineScreen(TaskScreen["str | None"]):
    """Python-path input + live command preview. Result: confirmed command."""

    CSS = """
    StatusLineScreen { align: center middle; }
    #box { width: 100; max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    #preview { color: $success; margin: 1 0; }
    #note { color: $text-muted; margin-bottom: 1; }
    #global { color: $warning; margin-bottom: 1; }
    Button { margin-right: 2; }
    """

    def __init__(
        self, python3: str, renderer: str | None, *, global_tee: bool = False
    ) -> None:
        super().__init__()
        self._python3 = python3
        self._renderer = renderer
        self._global_tee = global_tee

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
            if self._global_tee:
                yield Static(
                    "Your user-level ~/.claude/settings.json statusline already "
                    "runs this repo's tee, so the usage cache refreshes in every "
                    "folder. A local command written here would override it "
                    "inside this repo — choose \"Use global\" to remove the "
                    "local statusLine instead.",
                    id="global",
                )
            yield Static("python3 for the statusline command:")
            yield Input(value=self._python3, id="python")
            # _command() queries #python, which is not mounted during
            # compose — build the initial preview from the same default.
            yield Static(
                build_statusline_command(self._python3, REPO_ROOT, self._renderer),
                id="preview",
            )
            yield Static(self._note_text(), id="note")
            with Horizontal():
                if self._global_tee:
                    yield Button("Use global", id="useglobal", variant="primary")
                    yield Button("Apply local", id="apply")
                else:
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

    @on(Button.Pressed, "#useglobal")
    def _use_global(self) -> None:
        self.finish(USE_GLOBAL)


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
            "`uv run config.py`."
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
        local = statusline_settings(LOCAL_SETTINGS_PATH)
        global_tee = uses_repo_tee(statusline_settings(USER_SETTINGS_PATH), REPO_ROOT)
        if global_tee and local is None:
            self.notify(
                "Statusline configured globally: ~/.claude/settings.json runs "
                "the tee, so the usage cache refreshes in every folder. Nothing "
                "to write locally (a local statusLine would override it)."
            )
            return
        recommended = build_statusline_command(python3, REPO_ROOT, renderer)
        if not global_tee and local == recommended:
            self.notify("Statusline already configured.")
            return
        command = await self.push_screen_wait(
            StatusLineScreen(python3, renderer, global_tee=global_tee)
        )
        if command is None:
            return
        if command == USE_GLOBAL:
            if remove_local_statusline(LOCAL_SETTINGS_PATH):
                self.notify(
                    "Local statusLine removed; the global "
                    "~/.claude/settings.json statusline applies here now."
                )
            else:
                self.notify("No local statusLine to remove.")
            return
        if merge_local_settings(LOCAL_SETTINGS_PATH, command):
            self.notify(f"Statusline written to {LOCAL_SETTINGS_PATH}.")
        else:
            self.notify("Statusline already configured.")


def main() -> int:
    result = SetupApp().run()
    return result if isinstance(result, int) else 1  # ctrl+q/crash → None → 1
