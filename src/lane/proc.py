"""Child-process execution that does not outlive its caller.

Every child this package spawns holds something expensive: the browser, a Sol
Pro message in flight, or a gate run. If the caller is interrupted and the child
is left behind, that spend continues with nobody to receive the result.

Killing the direct child is not enough: gates run under a shell and engines
start browsers, so the process is put in its own session and the whole group is
signalled.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

TERMINATE_GRACE_SECONDS = 10.0
# Read size for the tail path. Peak memory there is this plus twice the kept tail.
READ_CHUNK_BYTES = 65536
# Widest UTF-8 encoding of one character: how many bytes a kept character costs.
BYTES_PER_CHAR = 4

# A gate inherits nothing it was not given: its output is fed back into the next
# Sol Pro prompt, so an inherited token can leave the machine through a stack
# trace. LC_* and anything the operator names explicitly are added on top.
GATE_ENV_KEYS = ("HOME", "LANG", "PATH", "TERM", "TZ", "USER")


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()

    def detail(self) -> str:
        lines = (self.stderr or self.stdout or "").strip().splitlines()
        return lines[-1] if lines else "no detail"


def sanitized_env(extra_keys: tuple[str, ...] = ()) -> dict[str, str]:
    """Environment for an untrusted child whose output is forwarded onward."""
    keys = set(GATE_ENV_KEYS) | set(extra_keys)
    return {
        key: value
        for key, value in os.environ.items()
        if key in keys or key.startswith("LC_")
    }


def run(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: float | None = None, shell: bool = False, capture: bool = True) -> Completed:
    """Run *command* to completion, terminating its process group on interruption."""
    pipe = subprocess.PIPE if capture else None
    with subprocess.Popen(command, cwd=cwd, env=env, shell=shell, text=True,
                          stdout=pipe, stderr=pipe, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
    return Completed(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


def run_tail(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None,
             shell: bool = False, limit: int, timeout: float | None = None) -> Completed:
    """Run *command*, keeping only the last *limit* characters it printed.

    run() holds the child's entire output in memory and lets the caller slice a
    tail off afterwards; a gate that prints a gigabyte of test log takes the lane
    down before that slice happens. Here stdout and stderr are merged in the
    order they arrive, consumed in fixed-size chunks, and everything but the tail
    is dropped as it comes in.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            tail = _drain_tail(process.stdout, keep=limit * BYTES_PER_CHAR, deadline=deadline)
            # The wait needs the deadline too: a child that closes its output and
            # keeps running reaches EOF here and then never exits.
            process.wait(None if deadline is None else max(deadline - time.monotonic(), 0.0))
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
    text = tail.decode("utf-8", "replace").strip()
    return Completed(returncode=process.returncode, stdout=text[-limit:], stderr="")


def run_relay(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None,
              timeout: float | None = None) -> Completed:
    """Run *command*, echoing its merged output live and returning it whole.

    `run(capture=False)` shows progress but keeps nothing — the operator sees
    an engine die, and the next process down the line has no evidence of why.
    `run(capture=True)` keeps everything but shows nothing for the minutes a
    review takes. This is both: lines are relayed as they arrive, and the full
    merged text comes back for the caller to persist on failure.

    Output is bounded by the caller dropping what it does not need; an
    unbounded child is `run_tail`'s problem, not the review engine's.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(command, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            chunks: list[str] = []
            for line in process.stdout:
                chunks.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            process.wait(None if deadline is None else max(deadline - time.monotonic(), 0.0))
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
    return Completed(returncode=process.returncode, stdout="".join(chunks), stderr="")


def _drain_tail(stream, *, keep: int, deadline: float | None = None) -> bytes:
    """Read *stream* to EOF holding at most ~2x *keep* bytes at any moment.

    With a deadline, a stream that goes quiet without closing cannot pin the
    caller: the write end can outlive the child that was spawned with it.
    """
    buffer = bytearray()
    selector = selectors.DefaultSelector() if deadline is not None else None
    if selector is not None:
        selector.register(stream, selectors.EVENT_READ)
    try:
        while True:
            if selector is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(cmd="run_tail", timeout=0)
                chunk = stream.read1(READ_CHUNK_BYTES)
            else:
                chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                return bytes(buffer[-keep:])
            buffer += chunk
            if len(buffer) > 2 * keep:
                del buffer[:-keep]
    finally:
        if selector is not None:
            selector.close()


def _stop(process: subprocess.Popen) -> None:
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        process.wait()


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole session; fall back to the child alone."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        pass


def exit_on_sigterm() -> None:
    """Turn SIGTERM into SystemExit so child cleanup runs.

    Without this, a supervisor that terminates the CLI leaves the engine
    running: the default SIGTERM disposition skips every `finally`.
    """
    signal.signal(signal.SIGTERM, _raise_system_exit)


def _raise_system_exit(signum, frame):  # noqa: ARG001 - signal handler signature
    raise SystemExit(128 + signum)
