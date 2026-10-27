"""How long one spoken utterance may hold the speech queue.

Shared by the platform TTS backends rather than living in ``mac_tts``, which
``.claude/rules/linux.md`` forbids importing on Linux ("Never import
``mac_tts``, ``mac_stt`` or ``mac_voice_loop`` on Linux"). The reasoning is not
platform-specific: every backend drains one queue sequentially, so a subprocess
that never returns does not merely lose its own utterance -- it silences every
announcement after it, with nothing logged and nothing recovering.
"""

from __future__ import annotations

#: Slowest speech rate to budget for, in characters per second. Measured with
#: the real /usr/bin/say on a 2 000-character line: 103 s at the macOS default
#: rate of 190 (19.4 c/s) and 135 s at rate 100 (14.8 c/s). 7.5 is half the
#: slowest of those, so the budget below is roughly 2x what speech actually
#: takes. espeak-ng at its default rate is comfortably faster than that.
CHARS_PER_SECOND = 7.5

#: Floor, so a short utterance still gets a sane allowance.
TIMEOUT_FLOOR_S = 60.0

#: Ceiling. Without one the scaling defeats the purpose: this function's own
#: worst case -- ``narration`` batching five summaries into a ~10 kB utterance --
#: yields 1 363 s, so a `say` stuck on a dead audio device silenced every later
#: announcement for 23 minutes with nothing logged, which is the exact wedge the
#: timeout bounds. Costs legitimate speech nothing: ``POST /api/tts`` caps text
#: at 2 000 characters, which budgets 297 s.
TIMEOUT_CEILING_S = 300.0


def utterance_timeout(text: str) -> float:
    """The budget for speaking *text*, scaled by its length.

    Scaled rather than fixed. A flat 120 s looked generous and was not:
    ``_MAX_SUMMARY_LEN`` is 2 000 characters, which ``POST /api/tts`` itself
    allows, and that takes 103 s at the default rate and 121 s at rate 150 --
    killed mid-sentence. Worse, ``narration`` batches up to five project
    summaries into ONE utterance, so the swarm announcement the batching exists
    for can be ~10 kB, and a flat cap threw away most of it.

    The timeout exists to stop a speech process that never returns from
    silencing every later announcement, not to ration legitimate speech. The
    wedge it was added for -- a synthesiser reading stdin forever after its text
    was consumed as a flag -- is closed separately by ``--`` and
    ``stdin=DEVNULL``.
    """
    return min(
        TIMEOUT_CEILING_S,
        max(TIMEOUT_FLOOR_S, len(text) / CHARS_PER_SECOND + 30.0),
    )
