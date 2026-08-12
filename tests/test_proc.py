from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from lane import proc

MARKER = "lane-orphan-probe-marker"
SLEEPER = ["python3", "-c", f"import time; time.sleep(45)  # {MARKER}"]


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


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def spawn_with_grandchild(tmp_path, timeout=0.4):
    """Run a shell that starts a detached grandchild and records its pid."""
    pidfile = tmp_path / "grandchild.pid"
    script = (
        f"sleep 60 & echo $! > {pidfile}; "
        f"while [ ! -s {pidfile} ]; do sleep 0.01; done; sleep 60"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(script, shell=True, timeout=timeout)
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    return int(pidfile.read_text().strip())


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    """A gate runs under a shell and the engine starts a browser: killing only
    the direct child leaves the expensive part running."""
    grandchild = spawn_with_grandchild(tmp_path)

    for _ in range(30):
        if not alive(grandchild):
            break
        time.sleep(0.1)
    assert not alive(grandchild), f"grandchild {grandchild} outlived the call"


def test_a_timeout_kills_the_child_instead_of_orphaning_it():
    """An abandoned engine keeps driving the browser and spending a Pro message."""
    assert not marker_alive()

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(SLEEPER, timeout=0.3)

    for _ in range(20):
        if not marker_alive():
            break
        time.sleep(0.1)
    assert not marker_alive(), "the child outlived the call"


def test_sanitized_env_drops_inherited_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "C")

    env = proc.sanitized_env()

    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["LC_ALL"] == "C"


def test_sanitized_env_keeps_explicitly_named_keys(monkeypatch):
    monkeypatch.setenv("EA_JUDGE_API_KEY", "secret")

    assert proc.sanitized_env(("EA_JUDGE_API_KEY",))["EA_JUDGE_API_KEY"] == "secret"


def test_sleep_marker_is_visible_to_pgrep():
    """Guards the probe itself: a marker hidden in a shell comment never reaches
    the real child's argv, so the orphan test would pass while orphaning."""
    process = subprocess.Popen(SLEEPER)
    try:
        for _ in range(20):
            if marker_alive():
                break
            time.sleep(0.05)
        assert marker_alive()
    finally:
        process.kill()
        process.wait()


def test_an_interrupted_call_kills_the_child_too(monkeypatch):
    assert not marker_alive()
    real_communicate = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proc.run(SLEEPER)
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
