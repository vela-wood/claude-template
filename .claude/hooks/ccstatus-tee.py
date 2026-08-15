#!/usr/bin/env python3
"""Statusline tee: cache rate-limit payloads, then echo or render.

The statusLine command lives in the gitignored .claude/settings.local.json,
written by `uv run config.py`. Two modes:

- Pipe mode (default): echo the raw stdin bytes to stdout before anything
  else, so a cache bug can never blank or mangle a downstream renderer
  (e.g. ccstatusline). A dead/missing downstream consumer (BrokenPipeError)
  must never prevent the cache write.
- --render: after caching, print one plain stdlib-built line (model display
  name, cwd basename, 5h/7d used-percentages). This makes the tee
  self-sufficient when ccstatusline is not installed.

Caches the verbatim stdin bytes to ~/legal/ccstatus.json so hooks (which
never receive rate_limits) can read usage percentages. Freshness is the
file's mtime. Exits 0 always; a malformed payload never tracebacks.
"""
import json
import os
import sys
import tempfile
import time

CACHE = os.path.expanduser("~/legal/ccstatus.json")
THROTTLE_SECONDS = 10


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
        cache_dir = os.path.dirname(CACHE)
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".ccstatus-")
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def render_line(data: bytes) -> str:
    """One plain statusline from the payload; empty string on any problem."""
    parts: list[str] = []
    try:
        payload = json.loads(data)
        model = (payload.get("model") or {}).get("display_name")
        if isinstance(model, str) and model:
            parts.append(model)
        workspace = payload.get("workspace") or {}
        cwd = workspace.get("current_dir") or workspace.get("project_dir")
        if isinstance(cwd, str) and cwd:
            parts.append(os.path.basename(cwd.rstrip("/")) or cwd)
        rate_limits = payload.get("rate_limits") or {}
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            window = rate_limits.get(key) or {}
            pct = window.get("used_percentage")
            if isinstance(pct, (int, float)):
                parts.append(f"{label} {round(pct)}%")
    except Exception:
        pass
    return " | ".join(parts)


def main() -> int:
    data = sys.stdin.buffer.read()
    if "--render" in sys.argv[1:]:
        write_cache(data)
        try:
            print(render_line(data))
        except Exception:
            pass
    else:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            # Point stdout at devnull so the interpreter-shutdown flush of
            # the dead pipe can't print "Exception ignored" noise either.
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except OSError:
                pass
        write_cache(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Interpreter-shutdown flush of a dead pipe must not traceback.
        os._exit(0)
    except Exception:
        sys.exit(0)
