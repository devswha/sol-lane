"""Cross-process exclusion.

A `threading.Lock` only serializes one server's own requests. The things this
package guards are shared by every process on the machine: one CDP browser, one
vendored engine file, one plan file per repository. Two `lane` invocations in
two terminals would otherwise drive the same browser at the same time and each
harvest the other's answer.

The lock is a kernel object, not a file. `flock` locks an inode, so a lock file
that gets deleted or replaced while it is held is no lock at all: the next
process opens the new inode and walks straight in. That is not hypothetical here
— `.ai-bridge/drive.lock` lives in the worktree the implementer is editing, and
`~/.insane-review/` fills up with packs and gets cleaned. So the name is bound in
Linux's abstract socket namespace instead, where there is nothing on disk to
remove and the kernel releases the name when the holder dies.

The path is still the identity of the lock (and still gets a pid written beside
it, for humans reading `fuser`-style questions), but the path is documentation.
The guarantee is the bound name.
"""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

POLL_SECONDS = 0.2
# Abstract names are capped at 107 bytes; a digest keeps every path in range and
# keeps two paths from colliding by accident.
NAME_PREFIX = "lane-"
DIGEST_CHARS = 32


class LockBusy(Exception):
    """Another process holds the lock. Maps to exit code 1."""


def browser_lock_path() -> Path:
    """One lock per CDP browser profile, shared by review, serve, and drive."""
    return Path(os.environ.get("LANE_BROWSER_LOCK", Path.home() / ".insane-review" / "browser.lock"))


def abstract_name(path: Path) -> bytes:
    """The kernel-namespace name this path stands for.

    Derived from the resolved path so two spellings of one lock are one lock, and
    hashed so the 107-byte abstract-namespace limit is never the caller's problem.
    """
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:DIGEST_CHARS]
    return b"\0" + f"{NAME_PREFIX}{digest}".encode()


@contextmanager
def exclusive(path: Path, *, timeout: float | None = None, wait_log=None):
    """Hold an exclusive machine-wide lock for *path* for the duration of the block."""
    name = abstract_name(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = None if timeout is None else time.time() + timeout
        announced = False
        while True:
            try:
                sock.bind(name)
                break
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                if deadline is not None and time.time() >= deadline:
                    raise LockBusy(f"another process holds {path}") from None
                if wait_log is not None and not announced:
                    wait_log(f"waiting for {path.name}: another lane run holds it")
                    announced = True
                time.sleep(POLL_SECONDS)
        _record_holder(path)
        yield sock
    finally:
        # Closing the socket unbinds the name; nothing is left behind to go stale.
        sock.close()


def _record_holder(path: Path) -> None:
    """Best effort: who holds this, for a human looking at the tree.

    Never load-bearing. A read-only directory must not stop the lock from working.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass
