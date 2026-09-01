from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lane import locks

HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from lane import locks
    with locks.exclusive(__import__("pathlib").Path(sys.argv[1])):
        open(sys.argv[2], "w").write("held")
        time.sleep(float(sys.argv[3]))
    """
)

@pytest.fixture(autouse=True)
def isolate_kernel_locks(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(locks.LOCK_DIR_ENV, str(tmp_path / "kernel-locks"))


def start_holder(lock: Path, flag: Path, seconds: float) -> subprocess.Popen:
    src = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen([sys.executable, "-c", HOLDER.format(src=src),
                                str(lock), str(flag), str(seconds)])
    for _ in range(100):
        if flag.exists():
            return process
        time.sleep(0.05)
    process.kill()
    raise AssertionError("holder never acquired the lock")


def test_the_lock_excludes_another_process(tmp_path: Path):
    lock, flag = tmp_path / "browser.lock", tmp_path / "held"
    holder = start_holder(lock, flag, 1.0)
    try:
        with pytest.raises(locks.LockBusy, match="another process holds"):
            with locks.exclusive(lock, timeout=0):
                pass
    finally:
        holder.wait(10)


def test_the_lock_is_released_when_the_holder_exits(tmp_path: Path):
    lock, flag = tmp_path / "browser.lock", tmp_path / "held"
    holder = start_holder(lock, flag, 0.3)
    holder.wait(10)

    with locks.exclusive(lock, timeout=2) as held:
        assert held.fileno() >= 0


def test_waiting_is_announced_once(tmp_path: Path):
    lock, flag = tmp_path / "browser.lock", tmp_path / "held"
    messages: list[str] = []
    holder = start_holder(lock, flag, 0.6)
    try:
        with locks.exclusive(lock, timeout=10, wait_log=messages.append):
            pass
    finally:
        holder.wait(10)

    assert len(messages) == 1
    assert "browser.lock" in messages[0]


def test_the_lock_file_records_the_holder(tmp_path: Path):
    lock = tmp_path / "drive.lock"

    with locks.exclusive(lock):
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_browser_lock_path_is_overridable(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LANE_BROWSER_LOCK", str(tmp_path / "custom.lock"))

    assert locks.browser_lock_path() == tmp_path / "custom.lock"


def test_deleting_the_lock_file_does_not_release_the_lock(tmp_path: Path):
    """Sol Pro, reviewing this module 2026-08-13: flock locks an inode, so a lock
    file removed or replaced while held lets the next process straight in. The
    drive lock lives in the worktree the implementer edits, so this is reachable."""
    lock, flag = tmp_path / "browser.lock", tmp_path / "held"
    holder = start_holder(lock, flag, 1.5)
    try:
        lock.unlink()
        with pytest.raises(locks.LockBusy, match="another process holds"):
            with locks.exclusive(lock, timeout=0):
                pass
    finally:
        holder.wait(10)


def test_replacing_the_lock_file_does_not_release_the_lock(tmp_path: Path):
    lock, flag = tmp_path / "browser.lock", tmp_path / "held"
    holder = start_holder(lock, flag, 1.5)
    try:
        lock.unlink()
        lock.write_text("999999\n", encoding="utf-8")
        with pytest.raises(locks.LockBusy):
            with locks.exclusive(lock, timeout=0):
                pass
    finally:
        holder.wait(10)


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod does not make a directory read-only")
def test_the_lock_works_when_the_pid_file_cannot_be_written(tmp_path: Path):
    """The record beside the lock is for humans; it is not the lock."""
    directory = tmp_path / "readonly"
    directory.mkdir()
    lock = directory / "browser.lock"
    directory.chmod(0o500)
    try:
        with locks.exclusive(lock, timeout=0):
            assert not lock.exists(), "nothing could be written, and that is fine"
    finally:
        directory.chmod(0o700)


def test_two_spellings_of_one_path_are_one_lock(tmp_path: Path):
    direct = tmp_path / "browser.lock"
    indirect = tmp_path / "." / "browser.lock"

    assert locks.lock_name(direct) == locks.lock_name(indirect)


def test_different_paths_are_different_locks(tmp_path: Path):
    assert locks.lock_name(tmp_path / "a.lock") != locks.lock_name(tmp_path / "b.lock")


def test_a_long_path_still_has_a_bounded_lock_name(tmp_path: Path):
    deep = tmp_path / ("x" * 90) / ("y" * 90) / "drive.lock"

    name = locks.lock_name(deep)

    assert len(name) == len("lane-") + locks.DIGEST_CHARS + len(".lock")


def test_load_bearing_lock_is_outside_the_receipt_tree(tmp_path: Path):
    receipt = tmp_path / "mutable-worktree" / ".ai-bridge" / "drive.lock"

    with locks.exclusive(receipt):
        kernel_path = locks.kernel_lock_path(receipt)
        assert kernel_path.parent == tmp_path / "kernel-locks"
        assert kernel_path.is_file()
        assert kernel_path != receipt


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_kernel_lock_directory_and_file_are_private(tmp_path: Path):
    receipt = tmp_path / "browser.lock"

    with locks.exclusive(receipt):
        kernel_path = locks.kernel_lock_path(receipt)
        assert kernel_path.parent.stat().st_mode & 0o777 == 0o700
        assert kernel_path.stat().st_mode & 0o777 == 0o600
