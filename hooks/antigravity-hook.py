#!/usr/bin/env python3
"""VoxHerd's Stop hook for Antigravity CLI (`agy`), handed on to on-stop.py.

agy's hooks (documented inside the agy 1.2.10 binary) differ from Claude
Code's in three ways, and each one shaped this file:

- **They block the agent loop** -- "Hooks run synchronously and block the agent
  loop". A summary is a model call of several seconds, so it cannot run here:
  this answers at once and hands the turn to on-stop.py in its own session.
- **Their stdout is parsed as protojson**, which rejects unknown keys, and a
  Stop hook answering `"decision": "continue"` would keep the agent working.
  `{}` -- no decision -- is the one reply that can only mean "let it stop",
  so it is written before anything else can go wrong.
- **camelCase payloads and no SessionStart.** `conversationId` is the session
  id and `workspacePaths[0]` the project; the bridge registers the session
  from its first Stop.

install.sh puts this in ~/.gemini/config/hooks.json under the hook name
"voxherd". It must never fail agy: every path exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# agy's terminationReason -> the stop_reason the bridge turns into an activity.
_STOP_REASONS = {
    "model_stop": "end_turn",
    "error": "error",
    "max_steps_exceeded": "max_steps",
}


def translate(payload: object) -> dict | None:
    """agy's Stop payload as the input on-stop.py reads, or None if unusable."""
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("conversationId")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    workspaces = payload.get("workspacePaths")
    cwd = ""
    if isinstance(workspaces, list):
        cwd = next((w for w in workspaces if isinstance(w, str) and w.strip()), "")
    reason = _STOP_REASONS.get(str(payload.get("terminationReason") or ""), "end_turn")
    if payload.get("error"):
        reason = "error"
    translated = {
        "session_id": session_id.strip(),
        "cwd": cwd,
        "transcript_path": str(payload.get("transcriptPath") or ""),
        "stop_reason": reason,
    }
    # Every agy hook payload names the model -- "auto" unless one was chosen --
    # which matters here because one agy runs Gemini, Claude or GPT-OSS.
    model = payload.get("modelName")
    if isinstance(model, str) and model.strip():
        translated["model"] = model.strip()
    return translated


def hand_off(translated: dict) -> None:
    """Start on-stop.py on [translated], in a session of its own, and return."""
    here = os.path.dirname(os.path.abspath(__file__))
    detach: dict = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(here, "on-stop.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=translated["cwd"] if os.path.isdir(translated["cwd"]) else here,
        env=dict(os.environ, VOXHERD_HOOK_ASSISTANT="antigravity"),
        **detach,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(translated).encode())
    proc.stdin.close()


def main() -> int:
    raw = sys.stdin.read()
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    # on-stop.py sets this for the model CLI it runs; nothing to report then.
    if os.environ.get("VOXHERD_HOOK_RUNNING"):
        return 0
    try:
        translated = translate(json.loads(raw))
    except ValueError:
        return 0
    if translated is not None:
        try:
            hand_off(translated)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
