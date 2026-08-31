from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import tracemalloc
import uuid

import pytest

from lane import proc

POLL_SECONDS = 0.05
POLL_ATTEMPTS = 40


@pytest.fixture
def marker() -> str:
    return f"lane-orphan-probe-{uuid.uuid4().hex}"


def sleeper(marker: str) -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(45)", marker]


def marker_alive(marker: str) -> bool:
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    return bool(found.strip())


def wait_until_gone(marker: str) -> bool:
    for _ in range(POLL_ATTEMPTS):
        if not marker_alive(marker):
            return True
        time.sleep(POLL_SECONDS)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_output_and_detail_read_from_both_streams():
    result = proc.run(["sh", "-c", "echo out; echo err >&2"])

    assert result.returncode == 0
    assert "out" in result.output and "err" in result.output
    assert result.detail() == "err", "detail prefers stderr, which carries the failure reason"


def test_detail_falls_back_to_stdout():
    result = proc.run(["sh", "-c", "echo only-stdout; exit 3"])

    assert result.returncode == 3
    assert result.detail() == "only-stdout"


def spawn_with_grandchild(tmp_path, timeout=0.4):
    """Start a Python parent that records the PID of its child."""
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run([sys.executable, "-c", script], timeout=timeout)
    for _ in range(POLL_ATTEMPTS):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(POLL_SECONDS)
    return int(pidfile.read_text().strip())


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    grandchild = spawn_with_grandchild(tmp_path)

    for _ in range(POLL_ATTEMPTS):
        if not alive(grandchild):
            break
        time.sleep(POLL_SECONDS)
    assert not alive(grandchild), f"grandchild {grandchild} outlived the call"


def test_a_timeout_kills_the_child_instead_of_orphaning_it(marker):
    assert not marker_alive(marker)

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(sleeper(marker), timeout=0.3)

    assert wait_until_gone(marker), "the child outlived the call"


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


def test_sleep_marker_is_visible_to_pgrep(marker):
    process = subprocess.Popen(sleeper(marker))
    try:
        for _ in range(POLL_ATTEMPTS):
            if marker_alive(marker):
                break
            time.sleep(POLL_SECONDS)
        assert marker_alive(marker)
    finally:
        process.kill()
        process.wait(timeout=1)


def test_an_interrupted_call_kills_the_child_too(monkeypatch, marker):
    assert not marker_alive(marker)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(proc, "_drain_output", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proc.run(sleeper(marker))

    assert wait_until_gone(marker)


def test_sigterm_becomes_system_exit_so_cleanup_runs():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        proc.exit_on_sigterm()
        with pytest.raises(SystemExit) as caught:
            signal.raise_signal(signal.SIGTERM)
        assert caught.value.code == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)


FLOOD = [sys.executable, "-c", "import sys; [sys.stdout.write('x' * 1000000) for _ in range(64)]"]


def test_run_output_is_bounded_and_marks_truncation(monkeypatch):
    monkeypatch.setattr(proc, "MAX_OUTPUT_CHARS", 80)
    script = "import sys; print('a' * 200); print('b' * 200, file=sys.stderr)"

    result = proc.run([sys.executable, "-c", script])

    assert result.returncode == 0
    assert result.stdout.startswith(proc.TRUNCATION_NOTICE)
    assert result.stderr.startswith(proc.TRUNCATION_NOTICE)
    assert result.stdout.endswith("a" * 19 + "\n")
    assert result.stderr.endswith("b" * 19 + "\n")
    assert len(result.stdout) == len(result.stderr) == 80


def test_run_relay_output_is_bounded_but_relayed(monkeypatch, capsys):
    monkeypatch.setattr(proc, "MAX_OUTPUT_CHARS", 80)
    result = proc.run_relay([sys.executable, "-c", "import sys; sys.stdout.write('r' * 200); sys.stdout.flush()"])

    assert result.stdout.startswith(proc.TRUNCATION_NOTICE)
    assert result.stdout.endswith("r" * 20)
    assert len(result.stdout) == 80
    assert capsys.readouterr().out == "r" * 200


def test_multibyte_output_is_truncated_on_a_valid_text_boundary(monkeypatch):
    monkeypatch.setattr(proc, "MAX_OUTPUT_CHARS", 80)

    result = proc.run([
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('가' * 200); sys.stdout.flush()",
    ])

    assert result.stdout.startswith(proc.TRUNCATION_NOTICE)
    assert result.stdout.endswith("가")


def test_a_blocked_relay_sink_cannot_defeat_the_child_timeout(monkeypatch, marker):
    release = threading.Event()

    class Blocked:
        def write(self, text):
            release.wait(5)

        def flush(self):
            pass

    monkeypatch.setattr(proc.sys, "stdout", Blocked())
    script = "import sys, time; print('started', flush=True); time.sleep(45)"
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.run_relay([sys.executable, "-c", script, marker], timeout=0.3)
    finally:
        release.set()

    assert time.monotonic() - started < 2
    assert wait_until_gone(marker)


def test_a_successful_parent_cannot_leave_a_detached_group_member(marker):
    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)', sys.argv[1]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )

    result = proc.run([sys.executable, "-c", script, marker], timeout=2)

    assert result.returncode == 0
    assert wait_until_gone(marker)


def test_run_relay_times_out_on_partial_output_without_a_newline(marker):
    script = "import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(45)"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_relay([sys.executable, "-c", script, marker], timeout=0.3)

    assert time.monotonic() - started < 2
    assert wait_until_gone(marker)


def test_run_kills_sigterm_resistant_descendant_after_leader_exits(monkeypatch, tmp_path, marker):
    monkeypatch.setattr(proc, "TERMINATE_GRACE_SECONDS", 0.1)
    pidfile = tmp_path / "resistant.pid"
    child = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(45)"
    parent = (
        "import pathlib, subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}, {marker!r}]); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run([sys.executable, "-c", parent], timeout=0.3)

    child_pid = int(pidfile.read_text())
    assert wait_until_gone(marker)
    assert not alive(child_pid), "SIGTERM-resistant descendant outlived the call"


def test_run_tail_keeps_the_tail_of_both_streams():
    result = proc.run_tail(["sh", "-c", "echo out; echo err >&2; exit 2"], limit=4000)

    assert result.returncode == 2
    assert result.output.splitlines() == ["out", "err"]


def test_run_tail_returns_exactly_the_last_characters():
    result = proc.run_tail([sys.executable, "-c", "print('ab' * 5000)"], limit=100)

    assert result.output == "ab" * 50


def test_run_tail_memory_does_not_grow_with_the_childs_output():
    tracemalloc.start()
    try:
        result = proc.run_tail(FLOOD, limit=4000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert result.returncode == 0
    assert len(result.output) == 4000
    assert peak < 4 * 1024 * 1024, f"held {peak} bytes of a 64 MB output"


def test_run_tail_rejects_an_unbounded_limit():
    with pytest.raises(ValueError, match="limit must be positive"):
        proc.run_tail(["echo", "hi"], limit=0)


def test_an_interrupted_tail_call_kills_the_child_too(monkeypatch, marker):
    assert not marker_alive(marker)

    def interrupted(stream, *, keep, deadline=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(proc, "_drain_tail", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proc.run_tail(sleeper(marker), limit=4000)

    assert wait_until_gone(marker), "the child outlived the tail call"


def test_run_tail_kills_a_child_that_closes_its_output_and_keeps_living(marker):
    script = f"import os, time; os.close(1); os.close(2); time.sleep(45)  # {marker}"

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_tail([sys.executable, "-c", script], limit=4000, timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the wait was not bounded ({elapsed:.1f}s)"
    assert wait_until_gone(marker), "the child outlived the timeout"


def test_run_tail_times_out_on_a_stream_that_goes_quiet_without_closing(marker):
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)', {marker!r}]); print('first'); time.sleep(45)"

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_tail([sys.executable, "-c", script], limit=4000, timeout=1.0)

    assert wait_until_gone(marker)


def test_run_tail_without_a_timeout_still_reads_to_eof():
    result = proc.run_tail(["echo", "bounded"], limit=4000)

    assert result.output == "bounded"


def test_run_tail_with_a_timeout_that_is_not_reached_returns_normally():
    result = proc.run_tail(["echo", "quick"], limit=4000, timeout=30)

    assert (result.returncode, result.output) == (0, "quick")


def test_atomic_write_requires_a_basename(tmp_path):
    with pytest.raises(ValueError, match="basename"):
        proc.atomic_write_text(tmp_path, "../escape", "no")


def test_atomic_write_refuses_a_symlinked_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError, match="unsafe output directory"):
        proc.atomic_write_text(linked, "artifact", "no")


def test_agent_sandbox_masks_home_and_mounts_only_the_checkout_writable(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    state = tmp_path / "state"
    agent = tmp_path / "agent"
    agent.write_text("binary stand-in", encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(
        proc.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else str(agent),
    )

    command = proc.sandbox_command(
        ["agent", "-p", f"@{root / 'plan.md'}"],
        root,
        home,
        state,
    )

    assert ["--tmpfs", "/home"] in [
        command[index:index + 2] for index in range(len(command) - 1)
    ]
    assert ["--bind", str(root.resolve()), "/mnt/sol-lane/workspace"] in [
        command[index:index + 3] for index in range(len(command) - 2)
    ]
    assert "@/mnt/sol-lane/workspace/plan.md" in command


def test_agent_sandbox_fails_closed_without_bubblewrap(tmp_path, monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)

    with pytest.raises(OSError, match="requires bubblewrap"):
        proc.sandbox_command(
            ["agent"],
            tmp_path,
            tmp_path / "home",
            tmp_path / "state",
        )


def test_repair_sandbox_mounts_only_the_declared_subtree_writable(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    vendor = root / "vendor"
    vendor.mkdir(parents=True)
    agent = tmp_path / "agent"
    agent.write_text("binary stand-in", encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(
        proc.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else str(agent),
    )

    command = proc.sandbox_command(
        ["agent"],
        root,
        tmp_path / "home",
        tmp_path / "state",
        writable_paths=("vendor",),
    )

    triples = [command[index:index + 3] for index in range(len(command) - 2)]
    assert ["--ro-bind", str(root.resolve()), "/mnt/sol-lane/workspace"] in triples
    assert [
        "--bind",
        str(vendor.resolve()),
        "/mnt/sol-lane/workspace/vendor",
    ] in triples


def test_lane_state_rejects_nested_names(tmp_path):
    with pytest.raises(ValueError, match="basename"):
        proc.lane_state_path(tmp_path, "../session")
    with pytest.raises(ValueError, match="basename"):
        proc.trusted_state_path(tmp_path, "../receipt")
