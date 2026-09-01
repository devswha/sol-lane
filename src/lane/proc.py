"""Child-process execution that does not outlive its caller.

Every child this package spawns holds something expensive: the browser, a Sol
Pro message in flight, or a gate run. If the caller is interrupted and the child
is left behind, that spend continues with nobody to receive the result.

Killing the direct child is not enough: gates run under a shell and engines
start browsers, so the process is put in its own session and the whole group is
signalled.
"""

from __future__ import annotations

import codecs
import hashlib
import locale
import os
import queue
import signal
import shutil
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex

TERMINATE_GRACE_SECONDS = 10.0
# Read size for the tail path. Peak memory there is this plus twice the kept tail.
READ_CHUNK_BYTES = 65536
# Widest UTF-8 encoding of one character: how many bytes a kept character costs.
BYTES_PER_CHAR = 4
# Maximum text retained from each captured stream.  Output beyond this is
# represented by TRUNCATION_NOTICE followed by the most recent output.
MAX_OUTPUT_CHARS = 1_000_000
TRUNCATION_NOTICE = "\n[output truncated]\n"
SANDBOX_TMP = str(Path(os.sep) / "tmp")


def lane_state_path(root: Path, name: str) -> Path:
    """Return checkout-specific trusted state outside the mutable checkout."""
    if Path(name).name != name:
        raise ValueError("state name must be a basename")
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return base / "sol-lane" / identity / name


def trusted_state_path(root: Path, name: str) -> Path:
    """Return parent-only orchestration state that is never mounted into sandboxes."""
    if Path(name).name != name:
        raise ValueError("state name must be a basename")
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return base / "sol-lane" / "trusted" / identity / name


def sandbox_command(command: list[str], root: Path, home: Path, state: Path, *,
                    writable_paths: tuple[str, ...] | None = None) -> list[str]:
    """Wrap an agent command in a bubblewrap filesystem/process boundary."""
    sandbox_root = Path("/mnt/sol-lane/workspace")
    sandbox_state = Path("/mnt/sol-lane/state")
    bubblewrap = shutil.which("bwrap")
    executable = shutil.which(command[0])
    if bubblewrap is None or executable is None:
        raise OSError("secure agent execution requires bubblewrap and a resolvable executable")
    root = root.resolve(strict=True)
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    for directory in (home, state):
        if directory.is_symlink() or not directory.is_dir():
            raise OSError(f"unsafe sandbox directory: {directory}")

    interpreter: str | None = None
    interpreter_args: list[str] = []
    try:
        first_line = Path(executable).read_bytes().splitlines()[0].decode("utf-8")
    except (OSError, UnicodeDecodeError, IndexError):
        first_line = ""
    if first_line.startswith("#!"):
        shebang = shlex.split(first_line[2:].strip())
        if shebang and Path(shebang[0]).name == "env" and len(shebang) > 1:
            interpreter = shutil.which(shebang[1])
            interpreter_args = shebang[2:]
        elif shebang:
            interpreter = str(Path(shebang[0]).resolve(strict=True))
            interpreter_args = shebang[1:]
        if interpreter is None:
            raise OSError(f"could not resolve sandbox interpreter for {executable}")

    executable_path = Path(executable).resolve(strict=True)
    executable_prefix = executable_path.parent.parent
    if (executable_prefix / "lib").is_dir():
        executable_target = str(
            Path("/mnt/sol-lane/agent-runtime") / executable_path.relative_to(executable_prefix)
        )
        executable_mount = [
            "--dir", "/mnt/sol-lane/agent-runtime",
            "--ro-bind", str(executable_prefix), "/mnt/sol-lane/agent-runtime",
        ]
    else:
        executable_target = "/mnt/sol-lane/bin/agent"
        executable_mount = ["--ro-bind", executable, executable_target]

    rewritten = []
    for argument in command[1:]:
        prefix = "@" if argument.startswith("@") else ""
        value = argument[1:] if prefix else argument
        try:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(root):
                value = str(sandbox_root / path.relative_to(root))
            elif path.is_absolute() and path.is_relative_to(state):
                value = str(sandbox_state / path.relative_to(state))
        except (OSError, ValueError):
            pass
        rewritten.append(prefix + value)

    wrapped = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind", "/", "/",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/mnt",
        "--dir", "/mnt/sol-lane",
        "--dir", "/mnt/sol-lane/bin",
        *executable_mount,
        "--dir", "/mnt/sol-lane/home",
        "--bind", str(home), "/mnt/sol-lane/home",
        "--dir", "/mnt/sol-lane/state",
        "--bind", str(state), "/mnt/sol-lane/state",
        "--dir", "/mnt/sol-lane/workspace",
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--tmpfs", "/run/user",
        "--tmpfs", SANDBOX_TMP,
    ]
    if writable_paths is None:
        wrapped += ["--bind", str(root), str(sandbox_root)]
    else:
        wrapped += ["--ro-bind", str(root), str(sandbox_root)]
        for relative in writable_paths:
            source = (root / relative).resolve(strict=True)
            if not source.is_relative_to(root) or source.is_symlink():
                raise OSError(f"unsafe writable sandbox path: {relative}")
            wrapped += ["--bind", str(source), str(sandbox_root / relative)]
    wrapped += [
        "--chdir", str(sandbox_root),
        "--setenv", "HOME", "/mnt/sol-lane/home",
        "--setenv", "TMPDIR", SANDBOX_TMP,
        "--setenv", "XDG_CACHE_HOME", "/mnt/sol-lane/home/cache",
        "--setenv", "XDG_CONFIG_HOME", "/mnt/sol-lane/home/config",
        "--setenv", "XDG_DATA_HOME", "/mnt/sol-lane/home/data",
        "--setenv", "XDG_RUNTIME_DIR", "/mnt/sol-lane/home/runtime",
        "--setenv", "XDG_STATE_HOME", "/mnt/sol-lane/home/state",
        "--",
    ]
    if interpreter is not None:
        insertion = wrapped.index(executable_mount[-1]) + 1
        interpreter_path = Path(interpreter)
        prefix = interpreter_path.parent.parent
        if (prefix / "lib").is_dir():
            interpreter_target = str(Path("/mnt/sol-lane/python") / interpreter_path.relative_to(prefix))
            wrapped[insertion:insertion] = [
                "--dir", "/mnt/sol-lane/python",
                "--ro-bind", str(prefix), "/mnt/sol-lane/python",
            ]
        else:
            interpreter_target = "/mnt/sol-lane/bin/interpreter"
            wrapped[insertion:insertion] = [
                "--ro-bind", interpreter, interpreter_target,
            ]
        wrapped += [
            interpreter_target,
            *interpreter_args,
            executable_target,
            *rewritten,
        ]
    else:
        wrapped += [executable_target, *rewritten]
    return wrapped


def atomic_write_bytes(directory: Path, name: str, data: bytes, *, mode: int = 0o600) -> Path:
    """Atomically replace *name* beneath a real directory without following links."""
    if Path(name).name != name:
        raise ValueError("atomic write name must be a basename")
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise OSError(f"unsafe output directory {directory}: {error}") from error
    temporary = f".{name}.{token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return directory / name


def atomic_write_text(directory: Path, name: str, text: str, *, mode: int = 0o600) -> Path:
    return atomic_write_bytes(directory, name, text.encode("utf-8"), mode=mode)

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


@contextmanager
def _managed_process(command: list[str], *, cwd: Path | None, env: dict[str, str] | None,
                     stdout, stderr, allow_descendants: bool = False):
    """Spawn a child in an OS process container tracked for this context."""
    platform_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        **platform_options,
    )
    job_handle = None
    try:
        if os.name == "nt":
            from . import winjob

            try:
                job_handle = winjob.attach(process, kill_on_close=not allow_descendants)
            except BaseException:
                process.kill()
                process.wait()
                raise
            setattr(process, "_lane_job_handle", job_handle)
        with process:
            yield process
    finally:
        if job_handle is not None:
            from . import winjob

            winjob.close(job_handle)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: float | None = None, capture: bool = True,
        allow_descendants: bool = False) -> Completed:
    """Run *command* to completion with bounded captured output.

    Successful launchers may opt into persistent children (the dedicated
    browser started by ``--ensure-env``). Timeouts and interruptions still
    terminate the entire process container.
    """
    pipe = subprocess.PIPE if capture else None
    deadline = None if timeout is None else time.monotonic() + timeout
    with _managed_process(
        command,
        cwd=cwd,
        env=env,
        stdout=pipe,
        stderr=pipe,
        allow_descendants=allow_descendants,
    ) as process:
        try:
            if capture:
                stdout, stderr, stdout_cut, stderr_cut = _drain_output(
                    process, deadline=deadline,
                )
                stdout = _truncate_output(_decode_output(stdout), stdout_cut)
                stderr = _truncate_output(_decode_output(stderr), stderr_cut)
            else:
                process.wait(None if deadline is None else max(deadline - time.monotonic(), 0.0))
                stdout = stderr = ""
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
        if not allow_descendants and _group_exists(process):
            _stop(process)
    return Completed(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


def run_tail(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
             limit: int, timeout: float | None = None) -> Completed:
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
    with _managed_process(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        try:
            tail = _drain_tail(process.stdout, keep=limit * BYTES_PER_CHAR, deadline=deadline)
            # The wait needs the deadline too: a child that closes its output and
            # keeps running reaches EOF here and then never exits.
            process.wait(None if deadline is None else max(deadline - time.monotonic(), 0.0))
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
        if _group_exists(process):
            _stop(process)
    text = tail.decode("utf-8", "replace").strip()
    return Completed(returncode=process.returncode, stdout=text[-limit:], stderr="")


def run_relay(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
              timeout: float | None = None) -> Completed:
    """Run *command*, echoing merged output live and returning bounded output.

    `run(capture=False)` shows progress but keeps nothing — the operator sees
    an engine die, and the next process down the line has no evidence of why.
    `run(capture=True)` keeps everything but shows nothing for the minutes a
    review takes. This is both: lines are relayed as they arrive, and the full
    merged text comes back for the caller to persist on failure.

    Retained output is capped at MAX_OUTPUT_CHARS.  When the cap is exceeded,
    the result starts with TRUNCATION_NOTICE and retains the newest output;
    relaying itself remains unbounded so operators still see all progress.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    with _managed_process(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        try:
            stdout, _, cut, _ = _drain_output(process, deadline=deadline, relay=True)
        except BaseException:  # timeout, SIGINT, SIGTERM-turned-SystemExit
            _stop(process)
            raise
        if _group_exists(process):
            _stop(process)
    return Completed(returncode=process.returncode,
                     stdout=_truncate_output(_decode_output(stdout), cut), stderr="")


def _drain_output(process: subprocess.Popen, *, deadline: float | None,
                  relay: bool = False) -> tuple[bytes, bytes, bool, bool]:
    """Drain process pipes with bounded reader threads on every supported OS."""
    streams = ((process.stdout, "stdout"), (process.stderr, "stderr"))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    cut = {"stdout": False, "stderr": False}
    keep = MAX_OUTPUT_CHARS * BYTES_PER_CHAR
    relay_queue: queue.Queue[str] | None = queue.Queue(maxsize=64) if relay else None
    relay_stop = threading.Event()
    relay_writer = None
    errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()

    if relay_queue is not None:
        def write_relay():
            while not relay_stop.is_set() or not relay_queue.empty():
                try:
                    text = relay_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                except (OSError, ValueError):
                    return

        relay_writer = threading.Thread(target=write_relay, daemon=True)
        relay_writer.start()

    def emit_relay(text: str) -> None:
        if relay_queue is None or not text:
            return
        try:
            relay_queue.put_nowait(text)
        except queue.Full:
            pass

    def read_stream(stream, name: str) -> None:
        decoder = codecs.getincrementaldecoder(locale.getencoding())() if relay else None
        read = getattr(stream, "read1", stream.read)
        try:
            while chunk := read(READ_CHUNK_BYTES):
                if decoder is not None:
                    emit_relay(decoder.decode(chunk))
                buffer = buffers[name]
                buffer += chunk
                if len(buffer) > keep:
                    del buffer[:-keep]
                    cut[name] = True
            if decoder is not None:
                emit_relay(decoder.decode(b"", final=True))
        except BaseException as error:
            errors.put(error)

    readers = [
        threading.Thread(target=read_stream, args=(stream, name), daemon=True)
        for stream, name in streams
        if stream is not None
    ]
    for reader in readers:
        reader.start()

    try:
        remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
        process.wait(timeout=remaining)
        for reader in readers:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            reader.join(timeout=remaining)
            if reader.is_alive():
                raise subprocess.TimeoutExpired(cmd=process.args, timeout=0)
        if not errors.empty():
            raise errors.get()
    finally:
        relay_stop.set()
        if relay_writer is not None:
            remaining = 1.0 if deadline is None else max(deadline - time.monotonic(), 0.0)
            relay_writer.join(timeout=min(remaining, 1.0))
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), cut["stdout"], cut["stderr"]


def _truncate_output(output: str, was_cut: bool) -> str:
    """Apply the public character limit after bounded byte collection."""
    if not was_cut and len(output) <= MAX_OUTPUT_CHARS:
        return output
    if MAX_OUTPUT_CHARS <= len(TRUNCATION_NOTICE):
        return TRUNCATION_NOTICE[:MAX_OUTPUT_CHARS]
    return TRUNCATION_NOTICE + output[-(MAX_OUTPUT_CHARS - len(TRUNCATION_NOTICE)):]


def _decode_output(output: bytes) -> str:
    """Match subprocess text mode's locale-based decoding."""
    return output.decode(locale.getencoding(), errors="replace")


def _drain_tail(stream, *, keep: int, deadline: float | None = None) -> bytes:
    """Read *stream* to EOF in a bounded reader thread on every supported OS."""
    buffer = bytearray()
    errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()

    def read_stream() -> None:
        read = getattr(stream, "read1", stream.read)
        try:
            while chunk := read(READ_CHUNK_BYTES):
                buffer.extend(chunk)
                if len(buffer) > 2 * keep:
                    del buffer[:-keep]
        except BaseException as error:
            errors.put(error)

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
    reader.join(timeout=remaining)
    if reader.is_alive():
        raise subprocess.TimeoutExpired(cmd="run_tail", timeout=0)
    if not errors.empty():
        raise errors.get()
    return bytes(buffer[-keep:])


def _stop(process: subprocess.Popen) -> None:
    _signal_group(process, force=False)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        # Reap an exited leader while continuing to inspect its original group.
        try:
            process.wait(0)
        except subprocess.TimeoutExpired:
            pass
        if not _group_exists(process):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    if _group_exists(process):
        _signal_group(process, force=True)
    try:
        process.wait()
    except ChildProcessError:
        pass


def _signal_group(process: subprocess.Popen, *, force: bool) -> None:
    """Terminate the child's whole process container; fall back to its leader."""
    if os.name == "nt":
        handle = getattr(process, "_lane_job_handle", None)
        if handle is not None:
            from . import winjob

            winjob.terminate(handle, exit_code=137 if force else 1)
            return
        try:
            process.kill() if force else process.terminate()
        except ProcessLookupError:
            pass
        return

    signal_to_send = signal.SIGKILL if force else signal.SIGTERM
    try:
        # start_new_session makes the leader PID the process-group ID. Looking
        # up its current PGID fails once it exits while descendants may survive.
        os.killpg(process.pid, signal_to_send)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.send_signal(signal_to_send)
    except ProcessLookupError:
        pass


def _group_exists(process: subprocess.Popen) -> bool:
    if os.name == "nt":
        handle = getattr(process, "_lane_job_handle", None)
        if handle is not None:
            from . import winjob

            try:
                return winjob.active_processes(handle) > 0
            except OSError:
                pass
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def exit_on_sigterm() -> None:
    """Turn SIGTERM into SystemExit so child cleanup runs.

    Without this, a supervisor that terminates the CLI leaves the engine
    running: the default SIGTERM disposition skips every `finally`.
    """
    signal.signal(signal.SIGTERM, _raise_system_exit)


def _raise_system_exit(signum, frame):  # noqa: ARG001 - signal handler signature
    raise SystemExit(128 + signum)
