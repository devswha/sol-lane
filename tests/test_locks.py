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

    with locks.exclusive(lock, timeout=2) as handle:
        assert handle.tell() >= 0


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
