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
import signal
import subprocess
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
             shell: bool = False, limit: int) -> Completed:
    """Run *command*, keeping only the last *limit* characters it printed.

    run() holds the child's entire output in memory and lets the caller slice a
    tail off afterwards; a gate that prints a gigabyte of test log takes the lane
    down before that slice happens. Here stdout and stderr are merged in the
    order they arrive, consumed in fixed-size chunks, and everything but the tail
    is dropped as it comes in.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    with subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            tail = _drain_tail(process.stdout, keep=limit * BYTES_PER_CHAR)
            process.wait()
        except BaseException:  # SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
    text = tail.decode("utf-8", "replace").strip()
    return Completed(returncode=process.returncode, stdout=text[-limit:], stderr="")


def _drain_tail(stream, *, keep: int) -> bytes:
    """Read *stream* to EOF holding at most ~2x *keep* bytes at any moment."""
    buffer = bytearray()
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return bytes(buffer[-keep:])
        buffer += chunk
        if len(buffer) > 2 * keep:
            del buffer[:-keep]


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
