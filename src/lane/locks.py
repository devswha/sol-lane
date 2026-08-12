"""Cross-process exclusion.

A `threading.Lock` only serializes one server's own requests. The things this
package guards are shared by every process on the machine: one CDP browser, one
vendored engine file, one plan file per repository. Two `lane` invocations in
two terminals would otherwise drive the same browser at the same time and each
harvest the other's answer.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

POLL_SECONDS = 0.2


class LockBusy(Exception):
    """Another process holds the lock. Maps to exit code 1."""


def browser_lock_path() -> Path:
    """One lock per CDP browser profile, shared by review, serve, and drive."""
    return Path(os.environ.get("LANE_BROWSER_LOCK", Path.home() / ".insane-review" / "browser.lock"))


@contextmanager
def exclusive(path: Path, *, timeout: float | None = None, wait_log=None):
    """Hold an exclusive OS lock on *path* for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - released in finally
    try:
        deadline = None if timeout is None else time.time() + timeout
        announced = False
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.time() >= deadline:
                    raise LockBusy(f"another process holds {path}") from None
                if wait_log is not None and not announced:
                    wait_log(f"waiting for {path.name}: another lane run holds it")
                    announced = True
                time.sleep(POLL_SECONDS)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
