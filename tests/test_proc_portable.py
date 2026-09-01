from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from lane import proc


def descendant_command(marker: Path, *, child_delay: float, parent_delay: float) -> list[str]:
    child = (
        "import pathlib,sys,time; "
        "time.sleep(float(sys.argv[2])); "
        "pathlib.Path(sys.argv[1]).write_text('alive')"
    )
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen("
        "[sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
        "); "
        "time.sleep(float(sys.argv[4]))"
    )
    return [
        sys.executable,
        "-c",
        parent,
        child,
        str(marker),
        str(child_delay),
        str(parent_delay),
    ]


def test_run_captures_both_streams_without_a_shell():
    script = "import sys; print('out'); print('err', file=sys.stderr)"

    result = proc.run([sys.executable, "-c", script], timeout=10)

    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_timeout_kills_a_descendant_even_when_success_may_leave_one(tmp_path: Path):
    marker = tmp_path / "orphaned-child"

    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(
            descendant_command(marker, child_delay=1.0, parent_delay=30),
            timeout=0.25,
            allow_descendants=True,
        )

    time.sleep(1.3)
    assert not marker.exists(), "a descendant escaped the process container"


def test_successful_parent_cannot_leave_a_descendant_by_default(tmp_path: Path):
    marker = tmp_path / "contained-child"

    proc.run(descendant_command(marker, child_delay=0.25, parent_delay=0), timeout=10)

    time.sleep(0.75)
    assert not marker.exists(), "a successful child escaped without an explicit opt-in"


def test_successful_launcher_may_leave_the_browser_process(tmp_path: Path):
    marker = tmp_path / "persistent-browser"

    proc.run(
        descendant_command(marker, child_delay=0.25, parent_delay=0),
        timeout=10,
        allow_descendants=True,
    )

    time.sleep(0.75)
    assert marker.read_text(encoding="utf-8") == "alive"


def test_captured_output_remains_bounded(monkeypatch):
    monkeypatch.setattr(proc, "MAX_OUTPUT_CHARS", 80)
    script = "import sys; sys.stdout.write('x' * 1000000); sys.stderr.write('y' * 1000000)"

    result = proc.run([sys.executable, "-c", script], timeout=10)

    assert len(result.stdout) == len(result.stderr) == 80
    assert result.stdout.startswith(proc.TRUNCATION_NOTICE)
    assert result.stderr.startswith(proc.TRUNCATION_NOTICE)
