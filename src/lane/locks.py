"""Cross-process exclusion shared by every supported host.

The browser is a single-flight resource: two lane processes driving one CDP
profile can spend two subscription messages and harvest each other's answers.
The lock therefore has to be owned by the kernel and released when its process
dies.

The caller-supplied path remains the lock identity and the human-readable PID
receipt.  The load-bearing file lives in a private, stable per-user directory,
outside worktrees and artifact directories that lane or an implementer may
delete.  POSIX uses ``flock`` and Windows uses ``msvcrt.locking``; both are
released by the kernel when the descriptor closes or the process exits.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

POLL_SECONDS = 0.2
NAME_PREFIX = "lane-"
DIGEST_CHARS = 32
LOCK_DIR_ENV = "LANE_LOCK_DIR"


class LockBusy(Exception):
    """Another process holds the lock. Maps to exit code 1."""


def browser_lock_path() -> Path:
    """One lock per CDP browser profile, shared by review, serve, and drive."""
    return Path(os.environ.get("LANE_BROWSER_LOCK", Path.home() / ".insane-review" / "browser.lock"))


def lock_directory() -> Path:
    """Private directory containing the load-bearing kernel-lock files."""
    override = os.environ.get(LOCK_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "sol-lane" / "locks"
    if sys.platform == "darwin":  # pragma: no cover - exercised by the macOS CI matrix
        return Path.home() / "Library" / "Application Support" / "sol-lane" / "locks"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "sol-lane" / "locks"


def lock_name(path: Path) -> str:
    """Stable, bounded filename for every spelling of one lock identity."""
    identity = os.path.normcase(str(path.expanduser().resolve()))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:DIGEST_CHARS]
    return f"{NAME_PREFIX}{digest}.lock"


def kernel_lock_path(path: Path) -> Path:
    return lock_directory() / lock_name(path)


@contextmanager
def exclusive(path: Path, *, timeout: float | None = None, wait_log=None):
    """Hold an exclusive machine-wide lock for *path* for the duration of the block."""
    kernel_path = kernel_lock_path(path)
    descriptor = _open_kernel_lock(kernel_path)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        announced = False
        while True:
            try:
                _try_lock(handle.fileno())
                acquired = True
                break
            except OSError as error:
                if not _is_busy(error):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockBusy(f"another process holds {path}") from None
                if wait_log is not None and not announced:
                    wait_log(f"waiting for {path.name}: another lane run holds it")
                    announced = True
                time.sleep(POLL_SECONDS)
        _write_kernel_holder(handle)
        _record_holder(path)
        yield handle
    finally:
        if acquired:
            _unlock(handle.fileno())
        handle.close()


def _open_kernel_lock(path: Path) -> int:
    directory = path.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"lock directory is not a real directory: {directory}")
    if os.name != "nt":
        details = directory.stat(follow_symlinks=False)
        if details.st_uid != os.getuid():
            raise PermissionError(f"lock directory is not owned by this user: {directory}")
        if stat.S_IMODE(details.st_mode) != 0o700:
            directory.chmod(0o700)
    elif path.is_symlink():  # pragma: no cover - exercised by the Windows CI matrix
        raise OSError(f"lock file cannot be a symlink: {path}")

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise OSError(f"lock path is not a regular file: {path}")
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)
    return descriptor


def _try_lock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _is_busy(error: OSError) -> bool:
    busy_codes = {errno.EACCES, errno.EAGAIN}
    if hasattr(errno, "EDEADLK"):
        busy_codes.add(errno.EDEADLK)
    return error.errno in busy_codes


def _write_kernel_holder(handle) -> None:
    handle.seek(0)
    handle.write(f"{os.getpid()}\n".encode())
    handle.truncate()
    handle.flush()


def _record_holder(path: Path) -> None:
    """Best-effort PID receipt for humans; never the load-bearing lock."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass
