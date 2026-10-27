"""Antigravity CLI (`agy`) transcripts, rewritten into the shape on-stop.py reads.

agy writes one JSON object per *step* to
``~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/``
``transcript.jsonl`` and ``transcript_full.jsonl`` -- the Stop hook names the
latter (the format is documented inside the agy 1.2.10 binary itself): ``type``
is ``USER_INPUT`` for a prompt and ``PLANNER_RESPONSE`` for the agent's reply and
its ``tool_calls``, with snake_case keys and the reply text in ``content``.

A prompt's ``content`` is not what the user typed. Measured on 1.2.12's first
live turn, agy wraps it and appends blocks of its own::

    <USER_REQUEST>
    Reply with just the word pong.
    </USER_REQUEST>
    <ADDITIONAL_METADATA>
    The current local time is: ...
    </ADDITIONAL_METADATA>

on-stop.py takes a prompt's first line as the request, so without unwrapping
every agy turn was announced as a request for "<USER_REQUEST>".

on-stop.py's summary code reads Claude Code's shape -- ``message.role`` plus
content blocks -- so instead of teaching it a second format, the steps are
translated into that shape first. Steps it would not use (tool results, file
views, checkpoints) are dropped.

Stdlib only: on-stop.py imports this from the installed hooks directory.
"""

from __future__ import annotations

import json
import re

# agy's step types sometimes carry the enum's prefix; the documented form does not.
_STEP_PREFIX = "CORTEX_STEP_TYPE_"

# What the user typed, inside the wrapper agy puts around every prompt.
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)

# agy tool -> the Claude tool on-stop.py already reports on, the agy argument
# that holds what it reports, and the key Claude uses for it.
_TOOLS: dict[str, tuple[str, str, str]] = {
    "run_command": ("Bash", "CommandLine", "command"),
    "write_to_file": ("Write", "TargetFile", "file_path"),
    "replace_file_content": ("Edit", "TargetFile", "file_path"),
    "multi_replace_file_content": ("Edit", "TargetFile", "file_path"),
}


def _user_text(text: str) -> str:
    """A prompt as the user typed it: the inside of agy's ``<USER_REQUEST>``,
    without the metadata blocks after it. Unwrapped text passes as it is."""
    match = _USER_REQUEST.search(text)
    return match.group(1) if match else text


def _step_type(step: dict) -> str:
    kind = str(step.get("type") or "").upper()
    return kind.removeprefix(_STEP_PREFIX)


def _args(call: dict) -> dict:
    args = call.get("args", call.get("arguments"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _tool_use(call: object) -> dict | None:
    if not isinstance(call, dict):
        return None
    mapped = _TOOLS.get(str(call.get("name") or ""))
    if mapped is None:
        return None
    name, source, target = mapped
    value = _args(call).get(source)
    if not isinstance(value, str) or not value.strip():
        return None
    return {"type": "tool_use", "name": name, "input": {target: value}}


def to_claude_lines(lines: list[str]) -> list[str]:
    """agy transcript lines as Claude Code transcript lines, in order.

    Never raises: an unreadable line is skipped, like on-stop.py skips its own.
    """
    out: list[str] = []
    for line in lines:
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if not isinstance(step, dict):
            continue
        kind = _step_type(step)
        content = step.get("content")
        text = content if isinstance(content, str) else ""
        if kind == "USER_INPUT":
            text = _user_text(text)
            if text.strip():
                out.append(json.dumps({"type": "user", "message": {"role": "user", "content": text}}))
        elif kind == "PLANNER_RESPONSE":
            blocks: list[dict] = []
            if text.strip():
                blocks.append({"type": "text", "text": text})
            calls = step.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                use = _tool_use(call)
                if use is not None:
                    blocks.append(use)
            if blocks:
                out.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": blocks}}))
    return out
