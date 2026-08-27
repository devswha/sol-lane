"""Sandbox end-to-end: every process boundary is real, only the paid ones are fake.

The unit suite exercises the modules in-process with monkeypatched seams. These
tests cross the real boundaries instead — a real engine subprocess, a real HTTP
socket, a real fake-gjc executable, a real gate process — using the two seams
the lane already ships:

    LANE_ENGINE        → a stub with the engine's CLI surface (no CDP, no Pro)
    PATH-resolved gjc  → a script that edits the worktree like an implementer

Nothing here spends a subscription message or needs a browser. The one thing
this cannot cover is the ChatGPT DOM itself; that is live-only by nature.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from lane import cli
from lane import review as review_module
from lane import serve as serve_module

# The default browser lock is machine-global on purpose. A sandbox run must not
# queue behind a real Pro turn that happens to be in flight on this machine.
pytestmark = pytest.mark.usefixtures("isolated_browser_lock")


@pytest.fixture
def isolated_browser_lock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LANE_BROWSER_LOCK", str(tmp_path / "browser.lock"))


STUB_ENGINE = '''\
"""Engine stand-in: same CLI surface as pack_and_ask.py, no CDP, no Pro.

Behaviour is chosen by directives in the prompt, so one stub serves every test:
    MAKE_CALL   council mode replies with a tool_call fence
    MAKE_PLAN   council mode replies with an implementation plan
    REFUSE      the written/printed answer is a refusal page
"""
import argparse
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--target")
ap.add_argument("--include")
ap.add_argument("--model")
ap.add_argument("--require-model")
ap.add_argument("--max-wait")
ap.add_argument("--force-answer-after")
ap.add_argument("--no-project", action="store_true")
ap.add_argument("--delete-pack", action="store_true")
ap.add_argument("--council", action="store_true")
ap.add_argument("--ensure-env", action="store_true")
ap.add_argument("--prompt")
ap.add_argument("--harvest")
ap.add_argument("--continue-chat")
ap.add_argument("prompt_args", nargs="*")
args = ap.parse_args()

if args.ensure_env:
    print("STATUS ok sandbox")
    sys.exit(0)

prompt = args.prompt or " ".join(args.prompt_args)
tag = f"{int(time.time())}_{os.getpid()}"


def write_response(body):
    out = os.path.join(os.getcwd(), ".insane-review")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"response_sandbox_{tag}.md"), "w", encoding="utf-8") as f:
        f.write("# review\\n- 모델: GPT-5.6\\n\\n---\\n" + body + "\\n")
    with open(os.path.join(out, f"manifest_sandbox_{tag}.json"), "w", encoding="utf-8") as f:
        f.write('{"chat_url": "https://chatgpt.com/c/6a7d67cb-0000-4000-8000-%012d"}' % os.getpid())


REFUSAL = "이 콘텐츠는 표시할 수 없습니다 (Trusted Access)"

if args.council:
    text = prompt or ""
    if "REFUSE" in text:
        print(REFUSAL)
    elif "MAKE_CALL" in text:
        print('```tool_call\\n{"name": "read_file", "arguments": {"path": "src/app.py"}}\\n```')
    elif "MAKE_PLAN" in text:
        print("1. edit src/app.py so that value is 2\\n2. run the gate")
    else:
        print("SANDBOX-COUNCIL-ANSWER")
    sys.exit(0)

if args.harvest:
    write_response("harvested answer body from the bound conversation")
    sys.exit(0)

# review mode (--prompt): the engine writes files instead of printing
if "DIE" in (prompt or ""):
    print("⏳ 전송 중")
    print("❌ 컴포저를 찾을 수 없음 (composer selector matched nothing)")
    sys.exit(1)
write_response(REFUSAL if "REFUSE" in (prompt or "") else "the cache expiry holds; the lock covers the read path")
sys.exit(0)
'''


@pytest.fixture
def stub_engine(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "stub_engine.py"
    path.write_text(STUB_ENGINE, encoding="utf-8")
    monkeypatch.setenv("LANE_ENGINE", str(path))
    return path


@pytest.fixture
def online(monkeypatch):
    """The CDP probe is the one boundary the stub cannot answer over HTTP."""
    monkeypatch.setattr(review_module, "cdp_up", lambda *a, **k: True)


def fake_gjc(tmp_path: Path, monkeypatch, script_body: str) -> None:
    """Install an executable named gjc ahead of everything else on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gjc = bin_dir / "gjc"
    gjc.write_text(f"#!{sys.executable}\nimport sys\n{script_body}\n", encoding="utf-8")
    gjc.chmod(gjc.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


# ── lane review ──────────────────────────────────────────────────────────────

def test_review_saves_a_verified_response_through_a_real_engine_process(
        write_config, stub_engine, online, project_root: Path, capsys):
    config = write_config()

    assert cli.main(["--config", str(config), "review", "demo",
                     "does the cache expiry hold under concurrent access"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "response   " in out
    saved = next(project_root.glob(".insane-review/response_*.md"))
    assert "cache expiry holds" in saved.read_text(encoding="utf-8")


def test_review_rejects_a_refusal_page_end_to_end(
        write_config, stub_engine, online, project_root: Path, capsys):
    """The 2026-08-13 false success, replayed across real process boundaries."""
    config = write_config()

    assert cli.main(["--config", str(config), "review", "demo",
                     "please REFUSE this one"]) == cli.EXIT_DELIVERY

    err = capsys.readouterr().err
    assert "fail-closed" in err
    assert not list(project_root.glob(".insane-review/response_*.md")), \
        "a refusal page kept the response_ name"
    rejected = next(project_root.glob(".insane-review/rejected_*.md"))
    assert rejected.read_text(encoding="utf-8").startswith("# REJECTED")


def test_harvest_recovers_from_the_manifest_the_review_left_behind(
        write_config, stub_engine, online, project_root: Path, capsys):
    config = write_config()
    manifests = project_root / ".insane-review"
    manifests.mkdir()
    (manifests / "manifest_review_1.json").write_text(
        '{"chat_url": "https://chatgpt.com/c/6a7d67cb-cfb4-83ee-b43f-b2b3d842bb47"}',
        encoding="utf-8")

    assert cli.main(["--config", str(config), "harvest", "demo"]) == cli.EXIT_OK

    assert "response   " in capsys.readouterr().out
    saved = next(project_root.glob(".insane-review/response_*.md"))
    assert "harvested answer" in saved.read_text(encoding="utf-8")


# ── lane serve ───────────────────────────────────────────────────────────────

def test_serve_bridges_a_tool_call_through_a_real_engine_subprocess(stub_engine):
    """HTTP in, engine subprocess in the middle, OpenAI tool_calls out."""
    settings = serve_module.ServeSettings(engine=stub_engine, max_wait=30,
                                          heartbeat_seconds=0.2)
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve_module.make_handler(
        settings, log=lambda *a, **k: None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=30)
        connection.request("POST", "/v1/chat/completions", body=json.dumps({
            "stream": True,
            "messages": [{"role": "user", "content": "please MAKE_CALL"}],
            "tools": [{"type": "function", "function": {
                "name": "read_file", "parameters": {"type": "object"}}}],
        }), headers={"content-type": "application/json"})
        body = connection.getresponse().read().decode()
    finally:
        server.shutdown()
        server.server_close()

    events = [json.loads(line[6:]) for line in body.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    calls = [event for event in events
             if event["choices"][0].get("delta", {}).get("tool_calls")]
    assert calls, f"no tool_calls in stream: {body[:400]}"
    call = calls[0]["choices"][0]["delta"]["tool_calls"][0]["function"]
    assert call["name"] == "read_file"
    assert json.loads(call["arguments"]) == {"path": "src/app.py"}
    finishes = [event["choices"][0].get("finish_reason") for event in events
                if event["choices"][0].get("finish_reason")]
    assert finishes == ["tool_calls"]


# ── lane drive ───────────────────────────────────────────────────────────────

def drive_repo(project_root: Path) -> None:
    """A worktree whose gate is red until value becomes 2."""
    (project_root / "check.py").write_text(
        "import pathlib, sys\n"
        "sys.exit(0 if 'value = 2' in pathlib.Path('src/app.py').read_text() else 1)\n",
        encoding="utf-8")
    (project_root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")


HONEST_GJC = (
    "import pathlib\n"
    "pathlib.Path('src/app.py').write_text('value = 2\\n', encoding='utf-8')\n"
    "print('implemented')"
)
TAMPERING_GJC = (
    "import pathlib\n"
    "pathlib.Path('check.py').write_text('import sys; sys.exit(0)\\n', encoding='utf-8')\n"
    "print('gate is green now')"
)


def test_drive_runs_the_full_loop_with_a_real_implementer_process(
        write_config, stub_engine, tmp_path: Path, project_root: Path, monkeypatch, capsys):
    drive_repo(project_root)
    fake_gjc(tmp_path, monkeypatch, HONEST_GJC)
    config = write_config(extra=f'gate = "{sys.executable} check.py"\n')

    assert cli.main(["--config", str(config), "drive", "demo", "MAKE_PLAN"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "verdict    PASS" in out
    assert (project_root / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    plan = (project_root / ".ai-bridge" / "current-plan.md").read_text(encoding="utf-8")
    assert "value is 2" in plan


def test_drive_refuses_an_implementer_that_rewrites_the_gate(
        write_config, stub_engine, tmp_path: Path, project_root: Path, monkeypatch, capsys):
    """The freeze, exercised against a real process editing the real worktree."""
    drive_repo(project_root)
    fake_gjc(tmp_path, monkeypatch, TAMPERING_GJC)
    config = write_config(extra=f'gate = "{sys.executable} check.py"\n')

    assert cli.main(["--config", str(config), "drive", "demo", "MAKE_PLAN"]) == cli.EXIT_DELIVERY

    err = capsys.readouterr().err
    assert "check.py" in err and "rewritten" in err
    assert "the gate was not run" in err


# ── lane repair ──────────────────────────────────────────────────────────────

REPAIRING_GJC = (
    "import pathlib, sys\n"
    "# the honest repairer: read the brief, verify it names the evidence, report\n"
    "brief = next(pathlib.Path('.ai-bridge').glob('repair-brief_*.md')).read_text(encoding='utf-8')\n"
    "assert 'composer selector matched nothing' in brief, 'brief must carry the evidence'\n"
    "assert 'pack_and_ask.py' in brief, 'brief must name the target'\n"
    "print('repair session OK: brief read, evidence present')"
)


def test_a_failed_review_leaves_evidence_the_repairer_can_read(
        write_config, stub_engine, online, project_root: Path, capsys):
    """The engine's last words must survive the process that died."""
    config = write_config()

    assert cli.main(["--config", str(config), "review", "demo", "please DIE quietly"]) == cli.EXIT_DELIVERY

    logs = list(project_root.glob(".insane-review/failed_*.log"))
    assert logs, "a failed run wrote no evidence"
    text = logs[0].read_text(encoding="utf-8")
    assert "composer selector matched nothing" in text
    assert text.startswith("# failed review run"), "the header names the exit and reason"


def test_repair_hands_the_evidence_to_a_real_repairer_process(
        write_config, stub_engine, tmp_path: Path, project_root: Path, monkeypatch, capsys):
    config = write_config()
    manifests = project_root / ".insane-review"
    manifests.mkdir()
    (manifests / "failed_20260827_150000_deadbeef.log").write_text(
        "# failed review run 20260827_150000_deadbeef\n# exit 1, reason: no verified response\n"
        "❌ 컴포저를 찾을 수 없음 (composer selector matched nothing)\n", encoding="utf-8")
    fake_gjc(tmp_path, monkeypatch, REPAIRING_GJC)

    assert cli.main(["--config", str(config), "repair"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "brief      " in out
    assert "repair session OK" in out, "the repairer's report is the command's output"
