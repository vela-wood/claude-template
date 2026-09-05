"""Terminal progress helpers shared by the OCR entry points."""

import threading
import time

# A long request burst can show the user nothing for minutes, so a periodic
# line proves the run is alive.
HEARTBEAT_SECONDS = 30.0


def format_duration(seconds: float) -> str:
    """Compact m/s duration for progress lines ('45s', '2m05s')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


class Heartbeat:
    """Print a periodic 'still working' line for a step with no output.

    A daemon thread waiting on an Event behaves identically on Windows and
    POSIX (no select, no signals, no fork), and the Event makes shutdown
    immediate rather than waiting out the interval.
    """

    def __init__(self, label: str, interval: float = HEARTBEAT_SECONDS):
        self._label = label
        self._interval = interval
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        while not self._done.wait(self._interval):
            elapsed = format_duration(time.monotonic() - start)
            print(f"\t{self._label} ({elapsed} elapsed)", flush=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._done.set()
        self._thread.join(timeout=1.0)
