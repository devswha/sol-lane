"""Turn an engine failure into a gjc repair session.

The engine drives the ChatGPT DOM, so it breaks when the UI moves. Until now
the only recovery was an operator reading the console of a dead run and
patching by hand. This module is the sanctioned agent surface for that job:
`lane repair` gathers the failure evidence the run left behind, writes a brief
that names the file to fix, the invariants that must survive the fix, and the
verification ladder — then hands the brief to a gjc session that does the
work. The lane's own guarantees are not weakened: the repairer edits committed
source, pytest decides, and the live 판 that proves the DOM fix is one review
the operator can watch fail or pass.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .review import FAILURE_GLOB

BRIEF_RELPATH = ".ai-bridge/repair-brief.md"
SESSION_RELPATH = ".ai-bridge/repair-session"
REPAIR_LOCK = ".ai-bridge/repair.lock"
# Same budget as drive: a tool-executing gjc outlives its own answer, and the
# repairer runs the engine's probes too.
REPAIR_TIMEOUT_SECONDS = 1800

REPAIR_INSTRUCTION = (
    "Repair the CDP engine described in the attached brief. Reproduce, fix "
    "vendor/pack_and_ask.py, run the verification ladder in order, and report "
    "what changed and each ladder step's result."
)

INVARIANTS = """\
## Invariants — a repair that breaks these is not a repair

- Fail-closed stays fail-closed. Unverified model, unconfirmed attachment,
  empty response, refusal page, prompt echo: none of these may become a saved
  response or exit 0. When in doubt, fail.
- No new network fetches. The engine downloads nothing; a fix is local DOM
  logic only.
- One message per run still holds: sending is once, recovery is read-only
  (`--harvest`, `--continue-chat`). A retry after a send must not resend.
- The browser profile and its login are the operator's. Never automate login,
  never touch other profiles, never kill a browser you did not launch.
- Scope is vendor/pack_and_ask.py. If the evidence points at src/lane, stop
  and report instead of fixing both at once.
"""

LADDER = """\
## Verification ladder — cheapest first, stop at the first failure

1. `uv run pytest -q` — the whole suite, sandbox included.
2. `uv run lane doctor` — engine compiles, browser up, roots clean.
3. Free engine probe: `python3 vendor/pack_and_ask.py --check-env` —
   browser, login, and dependency status without spending anything.
4. The live 판 (spends exactly one Pro message):
   `uv run lane review lane "repair smoke: answer with the single word OK" --include "README.md"`
   A verified response file is the only proof the DOM path works again.
"""


@dataclass(frozen=True)
class RepairOutcome:
    brief: Path
    command: list[str]
    returncode: int
    report: str


class RepairError(Exception):
    """The repair session could not proceed. Maps to exit code 1."""


def newest_failure(roots: list[Path]) -> Path | None:
    """The newest failure evidence across the given project roots.

    Ordered by filename, not mtime: the name carries the wall-clock stamp the
    run wrote, and a copied or touched log does not get to outrank a real one.
    """
    logs: list[Path] = []
    for root in roots:
        directory = root / Path(FAILURE_GLOB).parent
        logs.extend(path for path in directory.glob(Path(FAILURE_GLOB).name) if path.is_file())
    return max(logs, key=lambda path: path.name, default=None)


def build_brief(evidence: Path, engine: Path, *, project_name: str) -> str:
    text = evidence.read_text(encoding="utf-8")
    return f"""\
# Engine repair brief

The review engine died fail-closed. Fix it.

## What happened

Evidence below is the engine's own last output from project `{project_name}`,
saved when the run failed. The header names the exit code and the lane's
rejection reason; the rest is the engine's output tail.

Target: `{engine}` — committed source in this repository, the single home of
the engine. Edit it directly. There are no patches and no upstream to track;
why each historical change exists is documented in
`docs/field-notes.md#패치별-유래` and the invariants below carry the reasons
forward.

{INVARIANTS}
{LADDER}
## Report format

End with: files changed (with a one-line why per file), each ladder step's
pass/fail with its observable result, and anything you could not verify. Do
not commit — the operator reviews the diff.

## Evidence

```
{text}
```
"""


def repair_command(root: Path, brief: Path) -> list[str]:
    """Command that makes gjc execute the repair brief.

    A lane-owned session directory, like drive's: the repairer never writes
    into a session the operator is using. There is no --continue yet — one
    brief, one session; a follow-up repair starts fresh with fresh evidence.
    The wall clock is bounded by the caller's proc.run timeout, as in drive.
    """
    return ["gjc", "-p", "--no-title", "--session-dir", str(root / SESSION_RELPATH),
            f"@{brief}", REPAIR_INSTRUCTION]


def run_repair(root: Path, brief: Path) -> RepairOutcome:
    """Run the repairer and return what it reported."""
    command = repair_command(root, brief)
    try:
        result = proc.run(command, cwd=root, timeout=REPAIR_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise RepairError(
            f"gjc did not finish within {REPAIR_TIMEOUT_SECONDS}s and was killed; "
            "the engine may hold a partial fix"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RepairError(f"gjc could not run: {error}") from error
    return RepairOutcome(brief=brief, command=command,
                         returncode=result.returncode, report=result.stdout.strip())

def write_brief(root: Path, text: str) -> Path:
    """Write the brief under a fresh name; old briefs are history."""
    directory = root / Path(BRIEF_RELPATH).parent
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = uuid.uuid4().hex[:8]
    path = directory / f"repair-brief_{stamp}_{tag}.md"
    path.write_text(text, encoding="utf-8")
    return path
