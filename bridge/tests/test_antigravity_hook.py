"""VoxHerd's hook for Antigravity CLI (`agy`), end to end without agy.

agy's hook contract is documented inside the agy 1.2.10 binary: named hooks
in ~/.gemini/config/hooks.json, a Stop event whose stdin is camelCase
protojson, a stdout that must parse as one JSON object, and hooks that block
the agent loop while they run. None of it can be exercised against agy
itself until it is signed in, so these hold the contract as documented:

- the hook answers ``{}`` at once, whatever it is fed;
- a Stop payload reaches on-stop.py, which reads agy's *step* transcript and
  posts a stop event naming the right session, project and model;
- the installer adds a "voxherd" hook without touching anyone else's.

When any of these break, agy either waits on VoxHerd or silently never
reports a finished turn.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agy_transcript = _load("agy_transcript", "agy_transcript.py")
hook = _load("antigravity_hook", "antigravity-hook.py")

_CID = "ec33ebf9-0cba-4100-8142-c61503f6c587"


def _step(kind: str, content: str = "", **extra) -> str:
    return json.dumps({"step_index": 0, "source": "MODEL", "type": kind, "status": "DONE",
                       "content": content, **extra})


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args}


# A prompt as agy 1.2.12 records it: wrapped, with metadata blocks after it.
_PROMPT = ("<USER_REQUEST>\nAdd retry logic to fetch\n</USER_REQUEST>\n"
           "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-27T15:46:37-07:00.\n"
           "</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
           "`Model Selection` from None to Gemini 3.8 Flash (High).\n</USER_SETTINGS_CHANGE>")

_TURN = [
    _step("USER_INPUT", _PROMPT, source="USER_EXPLICIT"),
    _step("PLANNER_RESPONSE", "", tool_calls=[
        _call("view_file", AbsolutePath="/w/fetch.py"),
        _call("write_to_file", TargetFile="/w/fetch.py", CodeContent="..."),
        _call("run_command", CommandLine="pytest -q"),
    ]),
    _step("PLANNER_RESPONSE",
          "Added retry logic to fetch.py and the tests pass. Next I would add jitter to the backoff."),
]


# ---------------------------------------------------------------------------
# The transcript, as on-stop.py reads transcripts
# ---------------------------------------------------------------------------

def test_a_turn_reads_as_claude_messages() -> None:
    lines = [json.loads(line) for line in agy_transcript.to_claude_lines(_TURN)]
    assert lines == [
        {"type": "user", "message": {"role": "user", "content": "Add retry logic to fetch"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/fetch.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": json.loads(_TURN[2])["content"]},
        ]}},
    ]


def test_prefixed_step_types_and_string_arguments_are_read_too() -> None:
    line = json.dumps({"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "tool_calls": [
        {"name": "replace_file_content", "arguments": json.dumps({"TargetFile": "/w/a.py"})},
    ]})
    [out] = agy_transcript.to_claude_lines([line])
    assert json.loads(out)["message"]["content"] == [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}},
    ]


def test_a_prompt_is_what_the_user_typed_not_agys_wrapper() -> None:
    [line] = agy_transcript.to_claude_lines([_step("USER_INPUT", _PROMPT)])
    assert json.loads(line)["message"]["content"] == "Add retry logic to fetch"
    # A prompt agy did not wrap is read as it is.
    [plain] = agy_transcript.to_claude_lines([_step("USER_INPUT", "Add jitter too")])
    assert json.loads(plain)["message"]["content"] == "Add jitter too"
    # Nothing typed inside the wrapper is nothing to report.
    assert agy_transcript.to_claude_lines([
        _step("USER_INPUT", "<USER_REQUEST>\n  \n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"),
    ]) == []


def test_what_a_summary_cannot_use_is_dropped() -> None:
    assert agy_transcript.to_claude_lines([
        _step("VIEW_FILE", "file body"),
        _step("CHECKPOINT"),
        _step("USER_INPUT", "   "),
        _step("PLANNER_RESPONSE", "", tool_calls=[_call("view_file", AbsolutePath="/w/x")]),
        _step("PLANNER_RESPONSE", "", tool_calls=[_call("run_command")]),
        "not json",
        "[1, 2]",
    ]) == []


# ---------------------------------------------------------------------------
# The Stop payload
# ---------------------------------------------------------------------------

def _payload(**over) -> dict:
    base = {
        "conversationId": _CID,
        "workspacePaths": ["/Users/dev/fetcher"],
        "transcriptPath": "/Users/dev/.gemini/antigravity-cli/brain/x/transcript.jsonl",
        "artifactDirectoryPath": "/Users/dev/.gemini/antigravity-cli/brain/x",
        "modelName": "gemini-3.8-pro",
        "executionNum": 1,
        "terminationReason": "model_stop",
        "error": "",
        "fullyIdle": True,
    }
    base.update(over)
    return base


def test_a_stop_payload_becomes_on_stop_input() -> None:
    assert hook.translate(_payload()) == {
        "session_id": _CID,
        "cwd": "/Users/dev/fetcher",
        "transcript_path": "/Users/dev/.gemini/antigravity-cli/brain/x/transcript.jsonl",
        "stop_reason": "end_turn",
        "model": "gemini-3.8-pro",
    }


@pytest.mark.parametrize(("over", "reason"), [
    ({"terminationReason": "model_stop"}, "end_turn"),
    ({"terminationReason": "max_steps_exceeded"}, "max_steps"),
    ({"terminationReason": "error", "error": "quota"}, "error"),
    ({"terminationReason": "model_stop", "error": "exit status 1"}, "error"),
    ({"terminationReason": "something new"}, "end_turn"),
])
def test_why_it_stopped(over: dict, reason: str) -> None:
    assert hook.translate(_payload(**over))["stop_reason"] == reason


def test_what_it_cannot_use() -> None:
    for bad in (None, [], "x", {}, _payload(conversationId=""), _payload(conversationId=7)):
        assert hook.translate(bad) is None
    lean = hook.translate(_payload(workspacePaths=None, modelName=""))
    assert lean["cwd"] == "" and "model" not in lean


# ---------------------------------------------------------------------------
# The process agy runs
# ---------------------------------------------------------------------------

def _run_hook(stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / "antigravity-hook.py")],
        input=stdin, capture_output=True, text=True, timeout=10, check=False,
        env=env if env is not None else {**os.environ, "VOXHERD_HOOK_RUNNING": "1"},
    )


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"conversationId": 5}',
                                   json.dumps(_payload())])
def test_the_hook_always_answers_an_empty_decision(stdin: str) -> None:
    # agy parses this as protojson; anything else, or "continue", would keep the
    # agent running or fail the hook. VOXHERD_HOOK_RUNNING stops a real hand-off.
    done = _run_hook(stdin)
    assert done.returncode == 0
    assert done.stdout == "{}\n"
    assert json.loads(done.stdout) == {}


@pytest.fixture
def sandbox(tmp_path: Path):
    """A HOME with an agy transcript, and a PATH whose curl records the POST."""
    home = tmp_path / "home"
    workspace = home / "projects" / "fetcher"
    workspace.mkdir(parents=True)
    logs = home / ".gemini" / "antigravity-cli" / "brain" / _CID / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    transcript = logs / "transcript.jsonl"
    transcript.write_text("\n".join(_TURN) + "\n")

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    posted = tmp_path / "posted.json"
    curl = fakebin / "curl"
    # Records the POST, then lingers like a slow bridge: a hook that waited
    # for on-stop.py would hold agy for all of it.
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"open({str(posted)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "time.sleep(4)\n"
        "print('{\"ok\": true}')\n"
    )
    curl.chmod(0o755)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VOXHERD_", "TMUX")) and k not in (
               "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}
    env.update(HOME=str(home), PATH=f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin",
               VOXHERD_MODELS_ENV=str(home / "no-models.env"))
    return {"home": home, "workspace": workspace, "transcript": transcript,
            "posted": posted, "env": env}


def _wait_for(path: Path, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert path.exists(), f"nothing was posted within {seconds}s"


def test_a_finished_turn_reaches_the_bridge(sandbox) -> None:
    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    started = time.monotonic()
    done = _run_hook(json.dumps(payload), env=sandbox["env"])
    answered_in = time.monotonic() - started
    assert done.stdout == "{}\n" and done.returncode == 0
    # on-stop.py is still working when agy gets its answer.
    assert answered_in < 3, f"the hook held agy for {answered_in:.1f}s"

    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    event = json.loads(argv[argv.index("-d") + 1])
    assert event["event"] == "stop"
    assert event["session_id"] == _CID
    assert event["project"] == "fetcher"
    assert event["project_dir"] == str(sandbox["workspace"])
    assert event["assistant"] == "antigravity"
    assert event["model"] == "gemini-3.8-pro"
    assert event["stop_reason"] == "end_turn"
    # Read from agy's own transcript: the agent's last words, first sentence.
    assert event["summary"] == "Added retry logic to fetch.py and the tests pass."

    # And its tool calls reached the summariser's context as files and commands.
    log = (sandbox["home"] / ".voxherd" / "logs" / "on-stop-debug.log").read_text()
    assert "Files: fetch.py" in log
    assert "Commands: pytest -q" in log
    # The request as typed -- on agy's first live turn this read "<USER_REQUEST>".
    assert "User request: Add retry logic to fetch" in log
    assert "<USER_REQUEST>" not in log


def test_a_later_turn_is_announced_as_itself(sandbox) -> None:
    """Measured on agy's first live session: with no model summary to hand,
    the fallback read the first good sentence of the last three replies,
    oldest first -- so turn two was announced as turn one."""
    second = [
        _step("USER_INPUT", "<USER_REQUEST>\nAdd jitter too\n</USER_REQUEST>", source="USER_EXPLICIT"),
        _step("PLANNER_RESPONSE",
              "Added jitter to the retry backoff in fetch.py. The tests still pass after the change."),
    ]
    sandbox["transcript"].write_text("\n".join(_TURN + second) + "\n")
    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    assert _run_hook(json.dumps(payload), env=sandbox["env"]).returncode == 0
    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    event = json.loads(argv[argv.index("-d") + 1])
    assert event["summary"] == "Added jitter to the retry backoff in fetch.py."


def test_a_later_turn_with_nothing_to_quote_is_not_announced_as_an_earlier_one(sandbox) -> None:
    second = [
        _step("USER_INPUT", "<USER_REQUEST>\nAdd jitter too\n</USER_REQUEST>", source="USER_EXPLICIT"),
        _step("PLANNER_RESPONSE", "Done, jitter added."),
    ]
    sandbox["transcript"].write_text("\n".join(_TURN + second) + "\n")
    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    assert _run_hook(json.dumps(payload), env=sandbox["env"]).returncode == 0
    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    assert json.loads(argv[argv.index("-d") + 1])["summary"] == "Task completed."


def test_a_later_turn_with_only_long_sentences_is_still_itself(sandbox) -> None:
    """The live case: turn two's reply had no sentence of 20-100 characters,
    so the fallback passed over it to turn one's."""
    long_reply = ("Running tests before committing catches bugs and regressions immediately, "
                  "before broken code enters the shared history and fails everyone's CI builds. "
                  "It also keeps every commit in the history a known-good point that a later "
                  "bisect can trust without rebuilding and retesting each one of them by hand.")
    second = [
        _step("USER_INPUT", "<USER_REQUEST>\nWhy run tests first?\n</USER_REQUEST>", source="USER_EXPLICIT"),
        _step("PLANNER_RESPONSE", long_reply),
    ]
    sandbox["transcript"].write_text("\n".join(_TURN + second) + "\n")
    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    assert _run_hook(json.dumps(payload), env=sandbox["env"]).returncode == 0
    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    summary = json.loads(argv[argv.index("-d") + 1])["summary"]
    assert summary.startswith("Running tests before committing catches bugs"), summary
    assert summary.endswith("…") and len(summary) <= 101


def test_a_quoted_reply_keeps_only_the_words_of_its_links(sandbox) -> None:
    """Measured on agy 1.4.7 under the default profile, where there is no model
    summary: the fallback quoted the reply as written, so the headset was handed
    "[`add()`](file:///.../calc.py#L1-L2)" to speak, and the cut at 100
    characters fell inside the second link."""
    reply = ("I have updated [`add()`](file:///w/calc.py#L1-L2) in "
             "[calc.py](file:///w/calc.py) to return `a + b`.")
    second = [
        _step("USER_INPUT", "<USER_REQUEST>\nFix add()\n</USER_REQUEST>", source="USER_EXPLICIT"),
        _step("PLANNER_RESPONSE", reply),
    ]
    sandbox["transcript"].write_text("\n".join(_TURN + second) + "\n")
    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    assert _run_hook(json.dumps(payload), env=sandbox["env"]).returncode == 0
    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    assert json.loads(argv[argv.index("-d") + 1])["summary"] == (
        "I have updated add() in calc.py to return a + b."
    )


def test_a_google_install_summarises_through_agy(sandbox) -> None:
    """MODEL_PROFILE=google: the summary comes from `agy -p`, which runs on the
    Google sign-in that Gemini CLI no longer accepts. agy writes file names as
    markdown links; the spoken summary keeps only their words."""
    fakebin = Path(sandbox["env"]["PATH"].split(os.pathsep)[0])
    seen = fakebin.parent / "agy-called.json"
    agy = fakebin / "agy"
    agy.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(seen)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:],"
        " 'hook_running': os.environ.get('VOXHERD_HOOK_RUNNING')}))\n"
        "print('Added retry logic to [fetch.py](file:///w/fetch.py) and the tests pass.')\n"
    )
    agy.chmod(0o755)
    profile = sandbox["home"] / "google.env"
    profile.write_text("VOXHERD_SUMMARY_CLI=agy\nVOXHERD_SUMMARY_MODEL=gemini-3.8-flash-low\n")
    env = {**sandbox["env"], "VOXHERD_MODELS_ENV": str(profile)}

    payload = _payload(workspacePaths=[str(sandbox["workspace"])],
                       transcriptPath=str(sandbox["transcript"]))
    assert _run_hook(json.dumps(payload), env=env).returncode == 0
    _wait_for(sandbox["posted"])
    argv = json.loads(sandbox["posted"].read_text())
    assert json.loads(argv[argv.index("-d") + 1])["summary"] == (
        "Added retry logic to fetch.py and the tests pass."
    )
    called = json.loads(seen.read_text())
    assert called["argv"][0] == "-p"
    assert called["argv"][-3:] == ["--model", "gemini-3.8-flash-low", "--disable-slash-commands"]
    # So agy's own Stop hook -- VoxHerd's, if installed -- stands down.
    assert called["hook_running"] == "1"


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------

_ENTRY = {"Stop": [{"type": "command",
                    "command": "python3 $HOME/.voxherd/hooks/antigravity-hook.py",
                    "timeout": 10}]}

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="install.sh needs bash and jq",
)


def _install(home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VOXHERD_", "MODEL_PROFILE"))}
    env.update(HOME=str(home), HOOK_AGENTS="antigravity")
    return subprocess.run(["bash", str(HOOKS / "install.sh")], capture_output=True,
                          text=True, timeout=60, env=env, check=False)


def _hooks_json(home: Path) -> Path:
    return home / ".gemini" / "config" / "hooks.json"


@needs_shell
def test_install_adds_a_named_voxherd_hook(tmp_path: Path) -> None:
    done = _install(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(_hooks_json(tmp_path).read_text()) == {"voxherd": _ENTRY}
    for script in ("antigravity-hook.py", "agy_transcript.py", "on-stop.py"):
        assert (tmp_path / ".voxherd" / "hooks" / script).is_file(), script


@needs_shell
def test_install_leaves_other_hooks_alone_and_replaces_its_own(tmp_path: Path) -> None:
    theirs = {"PostToolUse": [{"matcher": "run_command",
                               "hooks": [{"type": "command", "command": "./lint.sh"}]}]}
    path = _hooks_json(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"lint-checker": theirs,
                                "voxherd": {"Stop": [{"command": "old-voxherd.sh"}]}}))
    assert _install(tmp_path).returncode == 0
    first = path.read_text()
    assert json.loads(first) == {"lint-checker": theirs, "voxherd": _ENTRY}
    assert _install(tmp_path).returncode == 0
    assert path.read_text() == first, "a second install must change nothing"


@needs_shell
@pytest.mark.parametrize("body", ["", "\n  \n"])
def test_install_reads_an_empty_file_as_no_hooks(tmp_path: Path, body: str) -> None:
    path = _hooks_json(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    assert _install(tmp_path).returncode == 0
    assert json.loads(path.read_text()) == {"voxherd": _ENTRY}


@needs_shell
@pytest.mark.parametrize("body", ["{not json", "[]", '"hooks"'])
def test_install_will_not_overwrite_a_file_it_cannot_read(tmp_path: Path, body: str) -> None:
    path = _hooks_json(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    assert _install(tmp_path).returncode != 0
    assert path.read_text() == body
