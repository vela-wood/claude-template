#!/usr/bin/env python3
"""Statusline: cache rate-limit payloads, then render one ANSI line.

This file is the in-repo source. `uv run config.py` installs a copy to
the ``hooks`` directory below ``$CLAUDE_CONFIG_DIR`` when that non-empty
override is set, or below ``~/.claude`` otherwise. The user-level settings
file in that same directory points the statusLine command at the copy, so
the statusline and usage cache work account-wide, in every folder. Nothing
statusline-related lives in the repo's .claude/.

Caches the verbatim stdin bytes to ``ccstatus.json`` in that configuration
directory first, so hooks (which never receive rate_limits) can read usage
percentages — freshness is the file's mtime. macOS memory readings use a
30-second ``ccstatus.mem`` cache in the same directory. The script then
prints one stdlib-built statusline: context bar, model + effort + total
speed, cache timer + cached tokens, git branch, 5h usage, free memory.
Every segment is individually fail-safe; a missing field just drops that
segment. Arguments are ignored (older installs passed --render). Exits 0
always; a malformed payload never tracebacks.
"""
import ctypes
import json
import os
import sys
import tempfile
import time


def _config_dir() -> str:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return os.path.expanduser(configured)
    return os.path.expanduser("~/.claude")


CACHE = os.path.join(_config_dir(), "ccstatus.json")
MEMORY_CACHE = os.path.join(_config_dir(), "ccstatus.mem")
THROTTLE_SECONDS = 10
MEMORY_CACHE_TTL_SECONDS = 30


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically replace *path* after writing every byte of *data*."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd = None
    tmp = None
    raw = None
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".ccstatus-")
        raw = os.fdopen(fd, "wb", buffering=0)
        fd = None
        remaining = memoryview(data)
        while remaining:
            written = raw.write(remaining)
            if written is None or written <= 0 or written > len(remaining):
                raise OSError("atomic byte write made no progress")
            remaining = remaining[written:]
        raw.close()
        raw = None
        os.replace(tmp, path)
        tmp = None
    except BaseException:
        if raw is not None:
            try:
                raw.close()
            except BaseException:
                pass
        elif fd is not None:
            try:
                os.close(fd)
            except BaseException:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except BaseException:
                pass
        raise


def write_cache(data: bytes) -> None:
    """Cache verbatim bytes; every failure is swallowed (guard fails open)."""
    try:
        # Substring check, not a parse: garbage or rate-limit-less payloads
        # must never clobber a good cache.
        if b'"rate_limits"' not in data:
            return
        try:
            if time.time() - os.stat(CACHE).st_mtime < THROTTLE_SECONDS:
                return
        except OSError:
            pass
        _atomic_write_bytes(CACHE, data)
    except Exception:
        pass


# Mirrors .claude/ccstatusline.json: bright green/blue/red/cyan/white ANSI.
_RESET = "\x1b[0m"
TRANSCRIPT_TAIL_BYTES = 131072
CACHE_TTL_SECONDS = 300
CACHE_TTL_SAFETY = 5


def _c(code: int, text: str) -> str:
    return f"\x1b[{code}m{text}{_RESET}"


def _fmt_tokens(count: float) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(int(count))


def _parse_ts(value) -> "float | None":
    """ISO-8601 timestamp -> epoch seconds (py3.9: no 'Z' in fromisoformat)."""
    try:
        import datetime

        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return None


def _scan_transcript(path: str) -> dict:
    """One tail read serving both the cache timer and the speed widget.

    Returns {working, last_assistant (epoch), tokens, seconds}: `working`
    and `last_assistant` follow ccstatusline's reverse scan (a user entry
    after the last finished turn means a request is in flight); tokens and
    seconds sum assistant usage over merged user->assistant intervals.
    """
    state = {"working": False, "last_assistant": None, "tokens": 0, "seconds": 0.0}
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        offset = max(0, size - TRANSCRIPT_TAIL_BYTES)
        fh.seek(offset)
        tail = fh.read()
    if offset:  # drop the partial first line
        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
    entries = []
    for raw in tail.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain") is True:
            continue
        entries.append(entry)

    # Cache timer: newest-first scan.
    turn_finished = False
    for entry in reversed(entries):
        etype = entry.get("type")
        if etype == "assistant":
            turn_finished = True
            usage = (entry.get("message") or {}).get("usage") or {}
            cache_activity = (usage.get("cache_creation_input_tokens") or 0) > 0 or (
                usage.get("cache_read_input_tokens") or 0
            ) > 0
            if entry.get("isApiErrorMessage") is not True and cache_activity:
                ts = _parse_ts(entry.get("timestamp"))
                if ts is not None:
                    state["last_assistant"] = ts
                    break
        elif etype == "user" and not turn_finished:
            state["working"] = True
            break

    # Speed: oldest-first scan, merged user->assistant wall-clock intervals.
    intervals = []
    last_user_ts = None
    for entry in entries:
        if entry.get("isApiErrorMessage"):
            continue
        ts = _parse_ts(entry.get("timestamp"))
        etype = entry.get("type")
        if etype == "user":
            if ts is not None:
                last_user_ts = ts
        elif etype == "assistant":
            usage = (entry.get("message") or {}).get("usage")
            if not isinstance(usage, dict):
                continue
            state["tokens"] += (usage.get("input_tokens") or 0) + (
                usage.get("output_tokens") or 0
            )
            if ts is not None and last_user_ts is not None and ts > last_user_ts:
                intervals.append((last_user_ts, ts))
    merged_end = None
    for start, end in sorted(intervals):
        if merged_end is None or start > merged_end:
            state["seconds"] += end - start
            merged_end = end
        elif end > merged_end:
            state["seconds"] += end - merged_end
            merged_end = end
    return state


def _seg_context_bar(payload: dict) -> "str | None":
    pct = (payload.get("context_window") or {}).get("used_percentage")
    if not isinstance(pct, (int, float)):
        return None
    filled = max(0, min(10, round(pct / 10)))
    bar = "█" * filled + "░" * (10 - filled)
    return _c(92, f"[{bar}] {round(pct)}%")


def _seg_model_speed(payload: dict, transcript: dict) -> "str | None":
    bits = []
    model = (payload.get("model") or {}).get("display_name")
    if isinstance(model, str) and model:
        bits.append(_c(94, model))
    effort = (payload.get("effort") or {}).get("level")
    if isinstance(effort, str) and effort:
        bits.append(_c(91, effort))
    if transcript["seconds"] > 0 and transcript["tokens"] > 0:
        speed = transcript["tokens"] / transcript["seconds"]
        text = f"{speed / 1000:.1f}k t/s" if speed >= 1000 else f"{speed:.1f} t/s"
        bits.append("@ " + _c(94, text))
    return " ".join(bits) or None


def _seg_cache(payload: dict, transcript: dict) -> "str | None":
    bits = []
    if transcript["working"]:
        bits.append(_c(37, "Cache: HOT"))
    elif transcript["last_assistant"] is not None:
        remaining = max(
            0,
            CACHE_TTL_SECONDS
            - CACHE_TTL_SAFETY
            - (time.time() - transcript["last_assistant"]),
        )
        bits.append(_c(37, f"Cache: {int(remaining // 60)}:{int(remaining % 60):02d}"))
    usage = (payload.get("context_window") or {}).get("current_usage") or {}
    cached = (usage.get("cache_read_input_tokens") or 0) + (
        usage.get("cache_creation_input_tokens") or 0
    )
    if cached:
        bits.append("@ " + _c(37, _fmt_tokens(cached)))
    return " ".join(bits) or None


def _seg_git_branch(payload: dict) -> "str | None":
    """Branch from .git/HEAD (no subprocess); hidden outside a repo."""
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    d = cwd
    while True:
        git_path = os.path.join(d, ".git")
        if os.path.exists(git_path):
            break
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    if os.path.isfile(git_path):  # worktree: "gitdir: <path>"
        with open(git_path, encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
        if not line.startswith("gitdir:"):
            return None
        git_path = line.split(":", 1)[1].strip()
        if not os.path.isabs(git_path):
            git_path = os.path.join(d, git_path)
    with open(os.path.join(git_path, "HEAD"), encoding="utf-8", errors="replace") as fh:
        head = fh.readline().strip()
    if head.startswith("ref: refs/heads/"):
        return _c(96, "⎇ " + head[len("ref: refs/heads/"):])
    return _c(96, "⎇ " + head[:8]) if head else None


def _seg_session_usage(payload: dict) -> "str | None":
    pct = ((payload.get("rate_limits") or {}).get("five_hour") or {}).get(
        "used_percentage"
    )
    if not isinstance(pct, (int, float)):
        return None
    return _c(97, f"5h {pct:.1f}%")


def _seg_extra_usage(payload: dict) -> "str | None":
    """Overage spend, if the payload ever carries it (ccstatusline pulls
    this from the authenticated OAuth usage API, which this script must not)."""
    extra = (payload.get("rate_limits") or {}).get("extra_usage") or {}
    cents = extra.get("used_cents")
    if isinstance(cents, (int, float)):
        return _c(91, f"${cents / 100:.2f}")
    return None


def _valid_memory_usage(used, total) -> bool:
    return (
        type(used) is int
        and type(total) is int
        and total > 0
        and 0 <= used <= total
    )


def _macos_vm_stat() -> "tuple[int, int] | None":
    """Return validated used and total physical bytes from ``vm_stat``."""
    import re
    import subprocess

    result = subprocess.run(
        ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=1
    )
    if result.returncode != 0:
        return None
    page_match = re.search(r"page size of (\d+) bytes", result.stdout)
    if page_match is None:
        return None
    page_size = int(page_match.group(1))
    pages = {
        match.group(1): int(match.group(2))
        for match in re.finditer(
            r"^(Pages[^:]*):\s+(\d+)\.", result.stdout, re.MULTILINE
        )
    }
    used = page_size * (
        pages.get("Pages active", 0)
        + pages.get("Pages wired down", 0)
        + pages.get("Pages occupied by compressor", 0)
    )
    total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    if not _valid_memory_usage(used, total):
        return None
    return used, total


def _read_memory_cache() -> "tuple[int, int] | None":
    try:
        with open(MEMORY_CACHE, encoding="utf-8") as fh:
            age = time.time() - os.fstat(fh.fileno()).st_mtime
            if age < 0 or age >= MEMORY_CACHE_TTL_SECONDS:
                return None
            cached = json.load(fh)
        if not isinstance(cached, dict):
            return None
        used = cached.get("used_bytes")
        total = cached.get("total_bytes")
        if not _valid_memory_usage(used, total):
            return None
        return used, total
    except Exception:
        return None


def _write_memory_cache(used: int, total: int) -> None:
    data = json.dumps(
        {"used_bytes": used, "total_bytes": total}, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write_bytes(MEMORY_CACHE, data)


def _macos_memory_usage() -> "tuple[int, int] | None":
    cached = _read_memory_cache()
    if cached is not None:
        return cached
    current = _macos_vm_stat()
    if current is None:
        return None
    try:
        _write_memory_cache(*current)
    except Exception:
        pass
    return current


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _windows_memory_usage() -> "tuple[int, int] | None":
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)
        )
    except Exception:
        return None
    if not succeeded:
        return None
    total = int(status.ullTotalPhys)
    used = total - int(status.ullAvailPhys)
    if not _valid_memory_usage(used, total):
        return None
    return used, total


def _seg_free_memory() -> "str | None":
    fmt = lambda b: f"{b / 2**30:.1f}G"  # noqa: E731
    if sys.platform.startswith("linux"):
        info = {}
        with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0]) * 1024
        return _c(94, f"{fmt(info['MemTotal'] - info['MemAvailable'])}/{fmt(info['MemTotal'])}")
    if sys.platform == "darwin":
        memory = _macos_memory_usage()
        if memory is None:
            return None
        used, total = memory
        return _c(94, f"{fmt(used)}/{fmt(total)}")
    if sys.platform == "win32":
        memory = _windows_memory_usage()
        if memory is None:
            return None
        used, total = memory
        return _c(94, f"{fmt(used)}/{fmt(total)}")
    return None


def render_line(data: bytes) -> str:
    """One ANSI statusline from the payload; empty string on any problem.

    Layout mirrors .claude/ccstatusline.json. Each segment is wrapped so a
    malformed field or unreadable transcript drops that segment only.
    """
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            return ""
    except Exception:
        return ""
    transcript = {"working": False, "last_assistant": None, "tokens": 0, "seconds": 0.0}
    try:
        transcript_path = payload.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            transcript = _scan_transcript(transcript_path)
    except Exception:
        pass
    segments = []
    for builder in (
        lambda: _seg_context_bar(payload),
        lambda: _seg_model_speed(payload, transcript),
        lambda: _seg_cache(payload, transcript),
        lambda: _seg_git_branch(payload),
        lambda: _seg_session_usage(payload),
        lambda: _seg_extra_usage(payload),
        lambda: _seg_free_memory(),
    ):
        try:
            segment = builder()
        except Exception:
            segment = None
        if segment:
            segments.append(segment)
    return " | ".join(segments)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    data = sys.stdin.buffer.read()
    write_cache(data)
    try:
        print(render_line(data))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Claude Code cancels in-flight statusline runs; the interpreter-
        # shutdown flush of a dead stdout must not traceback.
        os._exit(0)
    except Exception:
        sys.exit(0)
