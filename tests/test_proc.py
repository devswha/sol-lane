from __future__ import annotations

import signal
import subprocess
import time

import pytest

from lane import proc

MARKER = "lane-orphan-probe-marker"


def marker_alive() -> bool:
    found = subprocess.run(["pgrep", "-f", MARKER], capture_output=True, text=True).stdout
    return bool(found.strip())


def test_output_and_detail_read_from_both_streams():
    result = proc.run("echo out; echo err >&2", shell=True)

    assert result.returncode == 0
    assert "out" in result.output and "err" in result.output
    assert result.detail() == "err", "detail prefers stderr, which carries the failure reason"


def test_detail_falls_back_to_stdout():
    result = proc.run("echo only-stdout; exit 3", shell=True)

    assert result.returncode == 3
    assert result.detail() == "only-stdout"


def test_a_timeout_kills_the_child_instead_of_orphaning_it():
    """An abandoned engine keeps driving the browser and spending a Pro message."""
    assert not marker_alive()

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(f"sleep 45 # {MARKER}", shell=True, timeout=0.3)

    for _ in range(20):
        if not marker_alive():
            break
        time.sleep(0.1)
    assert not marker_alive(), "the child outlived the call"


def test_an_interrupted_call_kills_the_child_too(monkeypatch):
    assert not marker_alive()
    real_communicate = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proc.run(f"sleep 45 # {MARKER}", shell=True)
    monkeypatch.setattr(subprocess.Popen, "communicate", real_communicate)

    for _ in range(20):
        if not marker_alive():
            break
        time.sleep(0.1)
    assert not marker_alive()


def test_sigterm_becomes_system_exit_so_cleanup_runs():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        proc.exit_on_sigterm()
        with pytest.raises(SystemExit) as caught:
            signal.raise_signal(signal.SIGTERM)
        assert caught.value.code == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)
