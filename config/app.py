"""Setup hub for this repo: a Textual TUI (human-only — agents must never
run it) with three independent tasks:

1. Caption credentials → .env (config/env.py)
2. Sidecar naming preference → repo-root settings.json (repo_settings.py)
3. Statusline + usage cache → installed account-wide into ~/.claude
   (config/statusline.py): the statusline script (ccstatus.py) is copied to ~/.claude/hooks/
   and ~/.claude/settings.json points its statusLine at it. Nothing
   statusline-related is written inside the repo's .claude/; a leftover
   repo-local statusLine (which would shadow the account-wide one) is
   removed on install. The same task installs the usage-guard hooks
   (config/guard.py) into the repo's .claude/settings.local.json
   (LOCAL_SETTINGS_PATH — gitignored, per-machine).

Run with `uv run config.py` (a thin launcher for this module).
TUI-only; no plain fallback.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import httpx

import repo_settings
from repo_settings import RepoSettingsError

from .common import (
    ENV_FILE,
    LOCAL_SETTINGS_PATH,
    REPO_ROOT,
    USER_CLAUDE_DIR,
    USER_SETTINGS_PATH,
    SetupError,
    local_settings_path,
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
from .guard import (
    commit_guard_hooks,
    guard_hooks_present,
    merge_guard_hooks,
    prepare_guard_hooks,
)
from .statusline import (
    build_statusline_command,
    commit_user_statusline,
    detect_python3,
    install_state,
    install_user_statusline,
    installed_script,
    prepare_user_statusline,
    remove_local_statusline,
    statusline_settings,
    uses_installed_script,
    validate_python,
)

TASK_ORDER = ("env", "sidecar", "statusline")
TASK_LABELS = {
    "env": "Caption sign-in",
    "sidecar": "Document copy naming",
    "statusline": "Status bar & usage meter",
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
        noun = "credential" if len(values) == 1 else "credentials"
        rows["env"] = (
            f"ready ({len(values)} {noun} saved)" if values else "not set up yet"
        )
    except Exception as exc:
        rows["env"] = (
            "couldn't read your saved credentials — open this item to "
            f"re-enter them ({exc})"
        )
    try:
        rows["sidecar"] = (
            "copies are hidden (.contract.docx.md)"
            if repo_settings.read_sidecar_dotfiles()
            else "copies are visible (contract.docx.md)"
        )
    except Exception as exc:
        rows["sidecar"] = (
            "a settings file has a problem — ask for help, or delete "
            f"settings.json and run setup again ({exc})"
        )
    try:
        local_path = local_settings_path(Path(repo_root))
        state = install_state(Path(repo_root), USER_CLAUDE_DIR)
        wired = uses_installed_script(
            statusline_settings(USER_SETTINGS_PATH), USER_CLAUDE_DIR
        )
        local_override = statusline_settings(local_path) is not None
        if state == "missing" or not wired:
            rows["statusline"] = "not set up yet"
        elif local_override:
            rows["statusline"] = (
                "needs attention — this folder overrides your account-wide "
                "status bar (open this item to fix)"
            )
        elif state == "outdated":
            rows["statusline"] = (
                "needs attention — an updated status bar is ready to install"
            )
        elif not guard_hooks_present(local_path):
            rows["statusline"] = (
                "needs attention — the usage guard isn't hooked up in this "
                "folder (open this item to fix)"
            )
        else:
            rows["statusline"] = (
                "ready — status bar and usage guard installed, works in "
                "every folder"
            )
    except Exception as exc:
        rows["statusline"] = f"couldn't check this item ({exc})"
    return rows


def install_statusline_and_guard(python3: str) -> bool:
    """Validate both settings files, then install them in commit order.

    The return value reports only whether a repo-local statusLine override
    was removed.
    """
    statusline_prepared = prepare_user_statusline(
        REPO_ROOT,
        USER_CLAUDE_DIR,
        python3,
        settings_path=USER_SETTINGS_PATH,
    )
    guard_prepared = prepare_guard_hooks(
        LOCAL_SETTINGS_PATH,
        python3,
        REPO_ROOT,
    )

    local_settings = dict(guard_prepared.settings)
    removed_local_override = "statusLine" in local_settings
    if removed_local_override:
        del local_settings["statusLine"]
    guard_prepared = replace(
        guard_prepared,
        settings=local_settings,
        changed=guard_prepared.changed or removed_local_override,
    )

    commit_user_statusline(statusline_prepared)
    commit_guard_hooks(guard_prepared)
    return removed_local_override


# ---------------------------------------------------------------------------
# Textual TUI (human-only; agents must never run this)
# ---------------------------------------------------------------------------

from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.screen import Screen, ScreenResultType  # noqa: E402
from textual.theme import Theme  # noqa: E402
from textual.widgets import (  # noqa: E402
    Button,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option  # noqa: E402

# Company brand palette. Exact hexes need a truecolor terminal (256-color
# terminals quantize: the gold lands near #af8700). The palette has no
# red/green, so error/success stay conventional for visibility — muted warm
# shades chosen to sit well next to the gold and warm grays.
BRAND_THEME = Theme(
    name="velawood",
    primary="#CB9612",  # gold: borders, list highlight, primary buttons, spinner
    secondary="#D0CFC9",
    accent="#CB9612",
    warning="#CB9612",
    error="#CE5B4C",
    success="#8A9A5B",
    foreground="#F5F4F0",
    background="#000000",
    surface="#262324",  # midpoint of the brand blacks: input/button/list fills
    panel="#444142",
    dark=True,
    variables={
        "button-color-foreground": "#000000",  # black text on gold buttons
        "input-selection-background": "#CB9612 35%",
    },
)


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

    BINDINGS = [("escape", "cancel", "Back")]

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.finish(None)

    def action_cancel(self) -> None:
        self.finish(None)


class HubScreen(_DismissOnce, Screen[str]):
    """Result: task id ("env" | "sidecar" | "statusline") | "all" | "done"."""

    CSS = """
    #hub { width: 90; }
    #banner { color: $warning; margin-bottom: 1; }
    #rows { height: auto; margin-bottom: 1; }
    #buttons { height: auto; }
    """
    BINDINGS = [("escape", "done", "Close")]

    def __init__(self, rows: dict[str, str], update_notice: str) -> None:
        super().__init__()
        self._rows = rows
        self._update_notice = update_notice

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="hub"):
            banner = Static(self._update_notice, id="banner")
            banner.display = bool(self._update_notice)
            yield banner
            yield Static("Use ↑/↓ and Enter to open an item, or set up everything at once:")
            yield OptionList(
                *(
                    Option(
                        f"{TASK_LABELS[key]} — {self._rows.get(key, 'checking…')}",
                        id=key,
                    )
                    for key in TASK_ORDER
                ),
                id="rows",
            )
            with Horizontal(id="buttons"):
                yield Button("Set up everything", id="all", variant="primary")
                yield Button("Close", id="done")
        yield Footer()

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
    #box { width: 90; }
    #loading { height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="box"):
            yield Static(
                "Connect this toolkit to your Caption account:\n\n"
                f"1. Open this link in your browser: {SETUP_PAGE_URL}\n"
                "   (hold Cmd and click, or copy and paste it into your browser)\n"
                "2. Sign in if asked, then copy the setup code shown.\n"
                "3. Paste the code below and press Submit."
            )
            yield Input(password=True, placeholder="Paste your setup code here", id="token")
            loading = LoadingIndicator(id="loading")
            loading.display = False
            yield loading
            yield Static("", id="error")
            with Horizontal():
                yield Button("Submit", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")
        yield Footer()

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#loading", LoadingIndicator).display = busy
        self.query_one("#submit", Button).disabled = busy

    def _show_error(self, message: str) -> None:
        self.query_one("#error", Static).update(message)

    @work(exclusive=True)
    async def _fetch(self, token: str) -> None:
        try:
            payload = await fetch_setup_payload_async(token)
        except httpx.HTTPError:
            # Transport-level failure (offline, DNS, timeout) — the token is
            # probably fine, so don't blame it.
            if self.is_attached:
                self._set_busy(False)
                self._show_error(
                    "Couldn't reach the setup service. Check your internet "
                    "connection and try again."
                )
            return
        except SetupError as exc:
            if self.is_attached:
                self._set_busy(False)
                self._show_error(str(exc))
            return
        if self.is_attached and not self._finished:
            self.finish(payload)

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        if not token:
            self._show_error("Please paste a setup code first.")
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
    #box { width: 80; }
    """

    def __init__(self, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self._choices = choices

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="box"):
            yield Static(
                "Your account belongs to more than one organization. "
                "Pick the one to use here:"
            )
            yield RadioSet(
                *(
                    RadioButton(f"{name} ({org_id})" if org_id else name)
                    for name, org_id in self._choices
                ),
                id="orgs",
            )
            yield Static("", id="error")
            with Horizontal():
                yield Button("Select", id="select", variant="primary")
                yield Button("Cancel", id="cancel")
        yield Footer()

    @on(Button.Pressed, "#select")
    def _select(self) -> None:
        index = self.query_one("#orgs", RadioSet).pressed_index
        if index < 0:  # -1 when nothing selected
            self.query_one("#error", Static).update("Please pick an organization first.")
            return
        self.finish(index)


class SidecarScreen(TaskScreen["bool | None"]):
    """Result: sidecar_dotfiles value, or None on cancel."""

    CSS = """
    #box { width: 80; }
    """

    def __init__(self, current: bool | None) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="box"):
            yield Static(
                "When this toolkit reads a Word file or PDF, it saves a "
                "plain-text copy next to the original (for example "
                "contract.docx → contract.docx.md). How should those copies "
                "be named?"
            )
            yield RadioSet(
                RadioButton(
                    "Visible — contract.docx.md shows up next to the original (recommended)",
                    value=self._current is not True,
                ),
                RadioButton(
                    "Hidden — .contract.docx.md stays hidden in Finder and file lists",
                    value=self._current is True,
                ),
                id="style",
            )
            yield Static(
                "Your choice takes effect the next time documents are "
                "processed (uv run startup.py)."
            )
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")
        yield Footer()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        index = self.query_one("#style", RadioSet).pressed_index
        if index < 0:
            index = 0
        self.finish(index == 1)


class StatusLineScreen(TaskScreen["str | None"]):
    """Python-path input + live command preview. Result: the (validated)
    python3 path to install with, or None on cancel. python3=None means
    detection found nothing runnable — the user must type a path."""

    CSS = """
    #box { width: 100; }
    #preview { color: $success; margin: 0 0 1 0; }
    #note { color: $text-muted; margin-bottom: 1; }
    #warning { color: $warning; margin-bottom: 1; }
    #loading { height: 1; }
    """

    def __init__(self, python3: str | None, *, update: bool = False) -> None:
        super().__init__()
        self._python3 = python3 or ""
        self._update = update

    def _python_value(self) -> str:
        return self.query_one("#python", Input).value.strip() or self._python3

    def _preview_text(self, python3: str) -> str:
        if not python3:
            return "(waiting for a Python path above)"
        return build_statusline_command(python3, installed_script(USER_CLAUDE_DIR))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="box"):
            yield Static(
                "Status bar & usage meter — shows your Claude usage and "
                "session info in a bar at the bottom of Claude Code. It "
                "installs into your home folder (~/.claude), so it works "
                "in every folder, not just this one."
            )
            warning = Static(
                "Couldn't find Python automatically — type its full path "
                "below (for example C:\\Python312\\python.exe).",
                id="warning",
            )
            warning.display = not self._python3
            yield warning
            yield Static(
                "Where Python lives on this computer (pre-filled "
                "automatically — leave as is unless you know it's wrong):"
            )
            yield Input(
                value=self._python3,
                placeholder="Full path to Python (e.g. /usr/bin/python3)",
                id="python",
            )
            yield Static("Behind the scenes, this command runs the status bar:")
            # _python_value() queries #python, which is not mounted during
            # compose — build the initial preview from the same default.
            yield Static(self._preview_text(self._python3), id="preview")
            yield Static(
                "Nothing extra to install — no internet needed.", id="note"
            )
            loading = LoadingIndicator(id="loading")
            loading.display = False
            yield loading
            yield Static("", id="error")
            with Horizontal():
                yield Button(
                    "Install update" if self._update else "Install status bar",
                    id="apply",
                    variant="primary",
                )
                yield Button("Cancel", id="cancel")
        yield Footer()

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#loading", LoadingIndicator).display = busy
        self.query_one("#apply", Button).disabled = busy

    def _show_error(self, message: str) -> None:
        self.query_one("#error", Static).update(message)

    @on(Input.Changed, "#python")
    def _refresh_preview(self) -> None:
        self.query_one("#preview", Static).update(
            self._preview_text(self._python_value())
        )

    @work(exclusive=True)
    async def _validate_and_finish(self, candidate: str) -> None:
        ok = await asyncio.to_thread(validate_python, candidate)
        if not self.is_attached:
            return
        self._set_busy(False)
        if not ok:
            self._show_error(
                f"That Python didn't run ({candidate}). Check the path and "
                "try again — nothing was installed."
            )
            return
        if not self._finished:
            self.finish(candidate)

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        candidate = self._python_value()
        if not candidate:
            self._show_error("Please type the full path to Python first.")
            return
        self._show_error("")
        self._set_busy(True)
        self._validate_and_finish(candidate)


class SetupApp(App[int]):
    """Hub app: one @work async driver loops hub → task → refreshed hub."""

    TITLE = "Document Toolkit Setup"

    # Shared rules for every screen; per-screen CSS keeps only widths and
    # screen-specific widgets. Colors come from BRAND_THEME via $-variables.
    CSS = """
    Screen { align: center middle; }
    #hub, #box { max-width: 100%; height: auto; border: round $primary; padding: 1 2; }
    Button { margin-right: 2; }
    #error { color: $error; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.exit_code = 0
        self.update_notice = ""
        self.register_theme(BRAND_THEME)
        self.theme = BRAND_THEME.name

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
                        self.notify(
                            f"{TASK_LABELS[task]} didn't finish. You can try "
                            f"it again from the menu. ({exc})",
                            severity="error",
                        )
                        self.exit_code = 1
        except Exception as exc:  # a driver crash must still exit nonzero
            self.exit_code = 1
            try:
                self.notify(
                    "Something went wrong and setup had to stop. Nothing was "
                    f"damaged — you can close this window and try again. ({exc})",
                    severity="error",
                )
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
            "An update to this toolkit is available.\n"
            'To install it: press "Close" below, then run these two commands:\n'
            f"    git -C {REPO_ROOT} pull --ff-only\n"
            "    uv run config.py"
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
            "Naming preference saved. Existing copies will be renamed the "
            "next time documents are processed."
        )

    async def _task_statusline(self) -> None:
        state = install_state(REPO_ROOT, USER_CLAUDE_DIR)
        wired = uses_installed_script(
            statusline_settings(USER_SETTINGS_PATH), USER_CLAUDE_DIR
        )
        local_override = statusline_settings(LOCAL_SETTINGS_PATH) is not None
        guard_ok = guard_hooks_present(LOCAL_SETTINGS_PATH)
        if state == "current" and wired and not local_override and guard_ok:
            self.notify(
                "Your status bar and usage guard are already installed — "
                "they work in every folder. Nothing to change here."
            )
            return
        detected = await asyncio.to_thread(detect_python3)
        python3 = await self.push_screen_wait(
            StatusLineScreen(detected, update=state == "outdated")
        )
        if python3 is None:
            return
        removed = install_statusline_and_guard(python3)
        message = (
            "Status bar installed account-wide, and the usage guard is "
            "hooked up in this folder."
        )
        if removed:
            message += " (This folder's old override was removed.)"
        self.notify(message)


def main() -> int:
    result = SetupApp().run()
    return result if isinstance(result, int) else 1  # ctrl+q/crash → None → 1
