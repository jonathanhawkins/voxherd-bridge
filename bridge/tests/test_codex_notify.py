"""hooks/codex-notify.py under a model profile.

Codex reports a finished turn through its ``notify`` handler, with the turn's
last assistant message in argv. Before the ``openai`` profile existed that
message was the summary: its first line, cut at 120 characters. Now a chosen
profile summarises Codex turns the way ``on-stop.py`` summarises everyone
else's, and only a chosen one -- the stock install spends nothing new.

Runs the real script with a fake ``codex`` and a fake ``curl`` first on PATH,
under a throwaway HOME, so nothing reaches the developer's own bridge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks" / "codex-notify.py"

LAST_MESSAGE = (
    "I'll start by reading the config.\n\n"
    "Then I updated the parser so ISO dates round-trip, and all 40 tests pass."
)
OPENAI_ENV = (
    "VOXHERD_SUMMARY_CLI=codex\nVOXHERD_SUMMARY_MODEL=gpt-6-luna\n"
    "VOXHERD_NLU_CLI=codex\nVOXHERD_NLU_MODEL=gpt-6-luna\n"
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake binaries")


@pytest.fixture
def sandbox(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "codex-calls.jsonl"
    posted = tmp_path / "posted.json"

    codex = fakebin / "codex"
    codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        f"with open({str(calls)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin,\n"
        "        'hook_running': os.environ.get('VOXHERD_HOOK_RUNNING')}) + '\\n')\n"
        "if os.environ.get('FAKE_CODEX_FAIL'):\n"
        "    sys.exit(1)\n"
        "print('Updated the parser so ISO dates round-trip.')\n"
    )
    codex.chmod(0o755)
    curl = fakebin / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"open({str(posted)!r}, 'w').write(args[args.index('-d') + 1])\n"
        "print('{\"ok\": true}')\n"
    )
    curl.chmod(0o755)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VOXHERD_", "TMUX", "CODEX_HOME"))}
    env.update(HOME=str(home), PATH=f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin",
               VOXHERD_MODELS_ENV=str(home / "no-models.env"))
    return {"home": home, "env": env, "calls": calls, "posted": posted}


def _notify(sandbox, models_env: str | None = None, **extra_env: str) -> dict:
    env = dict(sandbox["env"], **extra_env)
    if models_env is not None:
        path = sandbox["home"] / "models.env"
        path.write_text(models_env)
        env["VOXHERD_MODELS_ENV"] = str(path)
    payload = {"type": "agent-turn-complete", "thread-id": "t-1", "turn-id": "u-1",
               "cwd": "/work/fetcher", "input-messages": ["fix dates"],
               "last-assistant-message": LAST_MESSAGE}
    done = subprocess.run([sys.executable, str(HOOK), json.dumps(payload)],
                          capture_output=True, text=True, timeout=60, env=env, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(sandbox["posted"].read_text())


def _calls(sandbox) -> list[dict]:
    if not sandbox["calls"].exists():
        return []
    return [json.loads(line) for line in sandbox["calls"].read_text().splitlines()]


def test_openai_profile_summarises_a_codex_turn_with_luna(sandbox) -> None:
    posted = _notify(sandbox, OPENAI_ENV)
    assert posted["summary"] == "Updated the parser so ISO dates round-trip."
    (call,) = _calls(sandbox)
    assert call["argv"][:2] == ["exec", "--ephemeral"]
    assert call["argv"][call["argv"].index("-m") + 1] == "gpt-6-luna"
    assert call["argv"][-2] == "--", "the prompt must follow a double dash"
    assert "ISO dates round-trip" in call["argv"][-1], "the turn's message is what gets summarised"
    assert call["stdin"] == "", "nothing but the prompt reaches the model"
    # So VoxHerd's own notify hook stands down if Codex ever runs it for this call.
    assert call["hook_running"] == "1"


def test_stock_install_keeps_the_free_first_line_summary(sandbox) -> None:
    """No profile chosen: Haiku is the default, and codex sessions never paid
    for it. Spending a model call per Codex turn has to be asked for."""
    posted = _notify(sandbox)
    assert posted["summary"] == "I'll start by reading the config."
    assert _calls(sandbox) == []


def test_a_failed_model_call_falls_back_to_the_first_line(sandbox) -> None:
    posted = _notify(sandbox, OPENAI_ENV, FAKE_CODEX_FAIL="1")
    assert posted["summary"] == "I'll start by reading the config."
    assert len(_calls(sandbox)) == 1


def test_a_quiet_worker_never_calls_the_model(sandbox) -> None:
    posted = _notify(sandbox, OPENAI_ENV, VOXHERD_QUIET="1")
    assert posted["skip_tts"] is True
    assert _calls(sandbox) == []


def test_a_missing_summary_cli_falls_back_quietly(sandbox) -> None:
    (Path(sandbox["env"]["PATH"].split(os.pathsep)[0]) / "codex").unlink()
    posted = _notify(sandbox, OPENAI_ENV)
    assert posted["summary"] == "I'll start by reading the config."
