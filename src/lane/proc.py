"""Child-process execution that does not outlive its caller.

Every child this package spawns holds something expensive: the browser, a Sol
Pro message in flight, or a gate run. If the caller is interrupted and the child
is left behind, that spend continues with nobody to receive the result — which
is exactly what happened when a `lane review` parent was killed and the engine
kept driving the browser for another ten minutes.
"""

from __future__ import annotations

import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

TERMINATE_GRACE_SECONDS = 10.0


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


def run(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: float | None = None, shell: bool = False, capture: bool = True) -> Completed:
    """Run *command* to completion, terminating it if anything interrupts us."""
    pipe = subprocess.PIPE if capture else None
    with subprocess.Popen(command, cwd=cwd, env=env, shell=shell, text=True,
                          stdout=pipe, stderr=pipe) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
    return Completed(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def exit_on_sigterm() -> None:
    """Turn SIGTERM into SystemExit so child cleanup runs.

    Without this, a supervisor that terminates the CLI leaves the engine
    running: the default SIGTERM disposition skips every `finally`.
    """
    signal.signal(signal.SIGTERM, _raise_system_exit)


def _raise_system_exit(signum, frame):  # noqa: ARG001 - signal handler signature
    raise SystemExit(128 + signum)
