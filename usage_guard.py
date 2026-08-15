#!/usr/bin/env python3
"""Opt-in, per-session usage guard fed by the statusline cache.

Reads ONLY ~/legal/ccstatus.json — the verbatim statusline payload cached by
~/.claude/hooks/ccstatus.py, which `uv run config.py` installs from this
repo's ccstatus.py (freshness = file mtime). No network, no OAuth, no
Keychain; stdlib only, so the hook hot path never needs `uv run`. Until that
install happens the cache never refreshes and every hook mode silently does
nothing (fails open) — the only visible complaint is in the manual mode.

At session start, if any usage window is >90%, a SessionStart hook injects
additionalContext asking Claude to offer a one-session stop-at-99% guard.
Off by default; declining or ignoring = no guard. Arming is keyed to
session_id (a flag file in ~/.claude/ccguard/), so it can never outlive the
session. Every hook mode ALWAYS exits 0 and fails open: a missing or stale
cache can warn, but can never block work.

Modes:
    --session-start        SessionStart hook (reads hook JSON on stdin)
    --arm SESSION_ID       arm the guard for one session
    --disarm SESSION_ID    change your mind mid-session
    --hook-json            PreToolUse hook (reads hook JSON on stdin)
    (default) / --json     manual check: uv run usage_guard.py

Exit codes (manual modes only):
    0 under threshold, 1 at/over threshold, 2 cache missing/stale/expired.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

CACHE = os.path.expanduser("~/legal/ccstatus.json")
GUARD_DIR = os.path.expanduser("~/.claude/ccguard")
SCRIPT_PATH = os.path.abspath(__file__)
FLAG_MAX_AGE_SECONDS = 7 * 24 * 3600
OFFER_PERCENT = 90.0

WINDOW_LABELS = {"five_hour": "session (5h)", "seven_day": "weekly (7d)"}


# ---------------------------------------------------------------------------
# Shared core: cache → gauges
# ---------------------------------------------------------------------------


def parse_resets_at(value):
    """Epoch seconds (int/float) or ISO 8601 string → aware datetime, else
    None. Unparseable/missing keeps the window; we guard on percentage alone
    and omit reset wording."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def read_gauges(max_age):
    """Return (gauges, stale).

    gauges: one dict per live rate-limit window in the cache — whatever keys
    are present (five_hour, seven_day, ...), skipping windows whose resets_at
    is in the past. [] means missing/unparseable cache or no surviving
    windows (treated identically). stale: cache mtime older than max_age.
    """
    try:
        mtime = os.stat(CACHE).st_mtime
        with open(CACHE, encoding="utf-8") as fh:
            payload = json.load(fh)
        rate_limits = payload["rate_limits"]
        if not isinstance(rate_limits, dict):
            return [], False
    except Exception:
        return [], False
    stale = time.time() - mtime > max_age

    now = dt.datetime.now(dt.timezone.utc)
    gauges = []
    for key, window in rate_limits.items():
        if not isinstance(window, dict):
            continue
        percent = window.get("used_percentage")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        reset = parse_resets_at(window.get("resets_at"))
        if reset is not None and reset <= now:
            continue
        gauges.append(
            {
                "kind": key,
                "label": WINDOW_LABELS.get(key, key),
                "percent": float(percent),
                "resets_at": window.get("resets_at"),
                "reset": reset,
            }
        )
    return gauges, stale


def local_reset_str(reset):
    return reset.astimezone().strftime("%Y-%m-%d %H:%M %Z") if reset else None


# ---------------------------------------------------------------------------
# Arm flags
# ---------------------------------------------------------------------------


def flag_path(session_id):
    # basename() so a hostile/garbled session_id cannot escape GUARD_DIR
    return os.path.join(GUARD_DIR, os.path.basename(str(session_id)))


def prune_flags():
    try:
        cutoff = time.time() - FLAG_MAX_AGE_SECONDS
        for name in os.listdir(GUARD_DIR):
            path = os.path.join(GUARD_DIR, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def read_session_id_from_stdin():
    try:
        return str(json.loads(sys.stdin.read()).get("session_id") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def mode_session_start(args):
    """SessionStart hook: offer the guard when any window is >90%."""
    prune_flags()
    session_id = read_session_id_from_stdin()
    gauges, stale = read_gauges(args.max_age)
    if not gauges or stale or not session_id:
        return 0
    binding = max(gauges, key=lambda g: g["percent"])
    if binding["percent"] <= OFFER_PERCENT:
        return 0

    reset_note = ""
    local_reset = local_reset_str(binding["reset"])
    if local_reset:
        reset_note = f"; resets {local_reset}"
    arm_cmd = f"/usr/bin/python3 {SCRIPT_PATH} --arm {session_id}"
    context = (
        f"Claude usage is at {binding['percent']:.0f}% of the "
        f"{binding['label']} window{reset_note}. On the first turn, ask the "
        "user via the AskUserQuestion tool whether to arm a usage guard that "
        "stops work at 99% FOR THIS SESSION ONLY. Options: "
        '"No, keep working (default; may spend extra usage)" and '
        '"Yes, stop me at 99%". Only on an explicit yes, run exactly: '
        f"{arm_cmd} . On no (or if the user ignores the question), do "
        "nothing — do not arm the guard."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


def mode_arm(session_id):
    os.makedirs(GUARD_DIR, exist_ok=True)
    with open(flag_path(session_id), "w", encoding="utf-8") as fh:
        fh.write(dt.datetime.now(dt.timezone.utc).isoformat() + "\n")
    print(
        f"usage guard armed for session {session_id}: tool calls stop at "
        f">=99% usage (disarm with --disarm {session_id})"
    )
    return 0


def mode_disarm(session_id):
    try:
        os.remove(flag_path(session_id))
        print(f"usage guard disarmed for session {session_id}")
    except FileNotFoundError:
        print(f"usage guard was not armed for session {session_id}")
    return 0


def mode_hook_json(args):
    """PreToolUse hook. Fast path first: unarmed sessions (the overwhelming
    default) pay one stat and never touch the cache."""
    session_id = read_session_id_from_stdin()
    if not session_id or not os.path.exists(flag_path(session_id)):
        return 0

    gauges, stale = read_gauges(args.max_age)
    if not gauges or stale:
        # Fail open, but tell the user their armed guard is blind.
        print(
            json.dumps(
                {
                    "systemMessage": (
                        "usage guard armed but cache stale/missing — "
                        "statusline cache pipeline may be broken"
                    )
                }
            )
        )
        return 0

    over = [g for g in gauges if g["percent"] >= args.threshold]
    if not over:
        return 0
    binding = max(over, key=lambda g: g["percent"])
    reason = (
        f"USAGE GUARD: {binding['label']} limit at {binding['percent']:.0f}% "
        f"(pause threshold {args.threshold:g}%). Stop work now. Do not start "
        "new tool calls or subagents."
    )
    local_reset = local_reset_str(binding["reset"])
    if local_reset:
        seconds = int(
            (binding["reset"] - dt.datetime.now(dt.timezone.utc)).total_seconds()
        )
        if seconds > 0:
            reason += (
                f" Resets at {local_reset} "
                f"({seconds // 3600}h {(seconds % 3600) // 60}m away). "
                "Tell the user, then call ScheduleWakeup for 3 minutes after reset."
            )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def mode_manual(args):
    now = dt.datetime.now(dt.timezone.utc)
    gauges, stale = read_gauges(args.max_age)
    if not gauges or stale:
        msg = (
            "usage cache stale (statusline cache pipeline may be broken)"
            if gauges
            else (
                f"no live usage data in {CACHE} (missing, unparseable, or "
                "all windows expired) — if the statusline was never "
                "installed, run `uv run config.py`"
            )
        )
        print(msg, file=sys.stderr)
        return 2

    binding = max(gauges, key=lambda g: g["percent"])
    over = binding["percent"] >= args.threshold
    seconds_until_reset = (
        int((binding["reset"] - now).total_seconds()) if binding["reset"] else None
    )

    if args.json:
        print(
            json.dumps(
                {
                    "binding_limit": binding["label"],
                    "percent": binding["percent"],
                    "threshold": args.threshold,
                    "over_threshold": over,
                    "resets_at": binding["resets_at"],
                    "seconds_until_reset": seconds_until_reset,
                    "checked_at": now.isoformat(),
                    "stale": stale,
                    "all_limits": [
                        {
                            "kind": g["kind"],
                            "label": g["label"],
                            "percent": g["percent"],
                            "resets_at": g["resets_at"],
                        }
                        for g in gauges
                    ],
                },
                indent=2,
            )
        )
    else:
        for gauge in sorted(gauges, key=lambda g: -g["percent"]):
            mark = "*" if gauge is binding else " "
            when = local_reset_str(gauge["reset"]) or "-"
            print(f"{mark} {gauge['percent']:6.2f}%  {gauge['label']:<16} resets {when}")
        print()
        if over:
            print(
                f"PAUSE: {binding['label']} at {binding['percent']:.2f}% "
                f"(threshold {args.threshold:g}%)"
            )
        else:
            print(
                f"OK: peak {binding['percent']:.2f}% "
                f"(threshold {args.threshold:g}%)"
            )
    return 1 if over else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session-start", action="store_true", help="SessionStart hook mode")
    ap.add_argument("--arm", metavar="SESSION_ID", help="arm the guard for one session")
    ap.add_argument("--disarm", metavar="SESSION_ID", help="disarm mid-session")
    ap.add_argument("--hook-json", action="store_true", help="PreToolUse hook mode")
    ap.add_argument("--json", action="store_true", help="machine-readable manual check")
    ap.add_argument("--threshold", type=float, default=99.0, help="stop percentage")
    ap.add_argument(
        "--max-age",
        type=float,
        default=21600.0,
        help="cache mtime older than this many seconds counts as stale (default 6h)",
    )
    args = ap.parse_args()

    # Hook modes: whole mode in try/except so they ALWAYS exit 0 — a crash
    # here must never wedge a session.
    if args.session_start:
        try:
            return mode_session_start(args)
        except Exception:
            return 0
    if args.hook_json:
        try:
            return mode_hook_json(args)
        except Exception:
            return 0
    if args.arm:
        return mode_arm(args.arm)
    if args.disarm:
        return mode_disarm(args.disarm)
    return mode_manual(args)


if __name__ == "__main__":
    sys.exit(main())
