"""A malformed frame is dropped. It does not take the connection with it.

The WebSocket receive loop catches only ``WebSocketDisconnect``, so anything
else raised while handling a frame escapes it and the client is disconnected --
losing its terminal subscriptions and its geometry lease, which leaves the
developer's tmux pane at panel width. That has now been found three times in
different shapes (a ValueError from a task id, an unhashable session id, a
non-object payload), so these assert the general property rather than the
instances.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from bridge.ws_handler import (
    _frame_string_fields_ok,
    _keys_would_answer,
    _live_choice_for,
    _live_decision_for,
)


class TestNonObjectFrames:
    @pytest.mark.parametrize("raw", ['["a","b"]', '"a string"', "42", "null", "true"])
    def test_a_frame_that_is_not_an_object_is_recognised(self, raw):
        # `dict(data)` raises ValueError on a list and TypeError on a scalar, so
        # the handler must reject these before it gets there.
        assert not isinstance(json.loads(raw), dict)


class TestHostileSessionIds:
    @pytest.mark.parametrize("sid", [[], {}, 5, None, 3.1, True, b"x"])
    def test_a_non_string_session_id_never_raises(self, sid):
        # An unhashable id made the cache lookup raise "unhashable type".
        assert _live_decision_for(sid) is None
        assert _live_choice_for(sid) is None


class TestHostileKeyPayloads:
    @pytest.mark.parametrize("payload", [
        {},
        {"keys": None},
        {"keys": 5},
        {"keys": []},
        {"key_sequence": "not a list"},
        {"key_sequence": [None, 5, {}]},
        {"key_sequence": None},
        {"literal": "yes", "keys": None},
        {"keys": "Enter", "key_sequence": ["Escape"]},
    ])
    def test_classification_never_raises(self, payload):
        assert isinstance(_keys_would_answer(payload), bool)

    def test_a_mixed_sequence_is_answering_if_any_key_answers(self):
        # Allowing a sequence because its first key is harmless would let
        # ["Escape", "Enter"] confirm the highlighted row.
        assert _keys_would_answer({"key_sequence": ["Escape", "Enter"]})
        assert _keys_would_answer({"key_sequence": ["Down", "Down", "Enter"]})
        assert not _keys_would_answer({"key_sequence": ["Escape", "PageUp"]})


class TestShiftTabApprovesAPlan:
    """`BTab` must count as answering, however much it looks like navigation.

    `choice_detector` anchors the ExitPlanMode prompt on its footer, "shift+tab
    to approve with this feedback" -- so shift-tab is a *confirmation* there, not
    a cursor move. The iPhone key bar offers it as "S-Tab", so a client really
    can send it; classifying it as harmless let a plan be approved with none of
    the commit path's checks.
    """

    @pytest.mark.parametrize("key", ["BTab", "btab", " BTab ", "Tab", "tab"])
    def test_tab_and_shift_tab_are_answering(self, key):
        assert _keys_would_answer({"keys": key}), f"{key!r} would approve a plan unchecked"
        assert _keys_would_answer({"key_sequence": [key]})

    @pytest.mark.parametrize("key", ["Escape", "escape", "C-c", "PageUp", "Up", "Down"])
    def test_dismissal_and_navigation_are_not_answering(self, key):
        assert not _keys_would_answer({"keys": key})

    @pytest.mark.parametrize("key", ["Enter", "enter", "C-m", "KPEnter", "Space", "BSpace"])
    def test_every_confirmation_spelling_is_answering(self, key):
        # C-m IS Enter in tmux; Space toggles a checkbox on a multi-select form.
        assert _keys_would_answer({"keys": key}), f"{key!r} could confirm a widget"


class TestKeyClassificationFailsClosed:
    """Across every key a client can send, only non-confirming ones pass.

    `_is_valid_tmux_key` accepts modifier runs (`C-`, `M-`, `S-`) in front of a
    named key or any single printable character, so the space is far wider than
    the hand-written allow-set. The property that matters is not that the set is
    complete -- an omission only refuses navigation, which is safe -- but that
    nothing in it can confirm a widget, and that no modifier variant slips
    through unexamined. `S-Tab` is the case that makes this worth asserting:
    it is shift-tab by another spelling, and shift-tab approves a plan.
    """

    @staticmethod
    def _every_sendable_key() -> list[str]:
        import itertools
        import string

        from bridge.ws_handler import _TMUX_NAMED_KEYS, _is_valid_tmux_key

        mods = ["", "C-", "M-", "S-", "C-M-", "C-S-", "M-S-"]
        names = sorted(_TMUX_NAMED_KEYS)
        chars = list(string.ascii_letters[:4] + string.digits[:4] + "!?/")
        candidates = [m + k for m, k in itertools.product(mods, names)]
        candidates += [m + c for m, c in itertools.product(mods, chars)]
        return [k for k in candidates if _is_valid_tmux_key(k)]

    def test_no_confirmation_spelling_is_ever_passed_through(self):
        confirms = {"enter", "c-m", "space", "tab", "btab", "s-tab", "kpenter"}
        leaked = [
            k for k in self._every_sendable_key()
            if not _keys_would_answer({"keys": k}) and k.lower() in confirms
        ]
        assert leaked == [], f"these could confirm a widget: {leaked}"

    def test_exactly_the_intended_keys_pass(self):
        passed = sorted(
            k.lower() for k in self._every_sendable_key()
            if not _keys_would_answer({"keys": k})
        )
        assert passed == sorted([
            "c-c", "down", "end", "escape", "home", "left",
            "npage", "pagedown", "pageup", "ppage", "right", "up",
        ]), "the passed-through set changed; confirm nothing here can confirm"

    def test_a_modifier_variant_is_not_silently_allowed(self):
        from bridge.ws_handler import _NON_ANSWERING_KEYS

        sneaked = [
            k for k in self._every_sendable_key()
            if not _keys_would_answer({"keys": k})
            and "-" in k and k.lower() not in _NON_ANSWERING_KEYS
        ]
        assert sneaked == [], f"modifier variants passed unexamined: {sneaked}"


class TestLiteralDoesNotBypassTheGate:
    """`literal` describes `keys`, and must not decide the gate for a sequence.

    The gate returned early on `literal`: with `literal: true` and no `keys` it
    answered `bool(None)` -> False and was skipped, while the handler went on to
    deliver `key_sequence` *interpreted* (it overrides literal to False there).
    So one field decided the security question and was ignored for delivery, and
    `{"literal": true, "key_sequence": ["Enter"]}` pressed Enter on a live
    permission dialog with none of the commit path's checks.

    docs/api.md already called these mutually exclusive ("Applies to `keys`
    only"; "`key_sequence` is always interpreted"), so the handler now refuses a
    frame carrying both, and this classifies on the keys that will really be
    sent.
    """

    @pytest.mark.parametrize("sequence", [
        ["Enter"], ["C-m"], ["Space"], ["S-Tab"], ["BTab"],
        ["Down", "Down", "Enter"], ["Escape", "Enter"],
    ])
    def test_literal_plus_a_confirming_sequence_is_answering(self, sequence):
        assert _keys_would_answer({"literal": True, "key_sequence": sequence}), (
            f"literal + {sequence} would reach a live widget unchecked"
        )

    @pytest.mark.parametrize("truthy", [True, 1, "yes", "false", [0]])
    def test_any_truthy_literal_is_handled(self, truthy):
        # The old shortcut triggered on truthiness, so a non-bool got there too.
        assert _keys_would_answer({"literal": truthy, "key_sequence": ["Enter"]})

    @pytest.mark.parametrize("sequence", [["Escape"], ["PageUp"], ["Up", "Down"]])
    def test_literal_plus_a_harmless_sequence_still_passes(self, sequence):
        assert not _keys_would_answer({"literal": True, "key_sequence": sequence})

    def test_literal_typing_is_still_answering(self):
        assert _keys_would_answer({"literal": True, "keys": "1\n"})
        assert _keys_would_answer({"literal": True, "keys": "approve"})


class TestTheLoopGuardsSessionIds:
    """The guard lives in the receive loop, not in each handler.

    A fourth instance of this file's docstring: ``{"session_id": []}`` with a
    ``choice_index`` reached ``sessions.get_session``, whose bare dict lookup
    raised ``unhashable type: 'list'``. That is not WebSocketDisconnect, so it
    escaped the loop and took the connection -- subscriptions and geometry lease
    -- with it. Each handler reads the field straight off the frame, so the
    check belongs where frames arrive.
    """

    @pytest.mark.parametrize("sid", [[], {}, 5, 3.1, True, ["a"], {"k": "v"}, None])
    def test_a_non_string_session_id_is_malformed(self, sid):
        assert not _frame_string_fields_ok({"session_id": sid})

    @pytest.mark.parametrize("frame", [
        {"session_id": "abc123"},
        {"session_id": ""},
        {},                                  # absent is fine; null is not
        {"type": "status_request"},
        {"tmux_session": "proj:0.0"},
    ])
    def test_a_usable_frame_is_not_rejected(self, frame):
        assert _frame_string_fields_ok(frame)

    def test_a_null_session_id_is_rejected(self):
        """It was allowed, on an untested claim that null means "absent".

        ``_handle_terminal_unsubscribe`` ends with ``session_id[:12]``, and
        ``data.get("session_id", "")`` returns None for an explicit null -- the
        default only covers a MISSING key. So ``{"session_id": null}`` raised
        ``'NoneType' object is not subscriptable`` out of the handler, which is
        exactly the connection-dropping class the gate exists to stop.
        """
        assert not _frame_string_fields_ok({"session_id": None})

    @pytest.mark.parametrize("field", ["tmux_session", "project", "task_id"])
    @pytest.mark.parametrize("value", [None, 5, [], {}])
    def test_the_other_string_fields_are_covered_too(self, field, value):
        """A one-field gate left its neighbours open.

        ``{"tmux_session": 5}`` raised "expected string or bytes-like object"
        out of ``_is_safe_tmux_target``, and ``{"tmux_session": {}}`` raised
        "unhashable type: 'dict'" -- same escape, different field.
        """
        assert not _frame_string_fields_ok({field: value})

    def test_the_guarded_set_matches_what_handlers_read_as_strings(self):
        """Keeps the list honest as handlers are added.

        Every ``data.get("<field>", "")`` in the module is a field read as a
        string, so it belongs in the gate -- the ``""`` default only covers a
        missing key, not a wrong type.
        """
        import re

        from bridge import ws_handler

        source = pathlib.Path(ws_handler.__file__).read_text()
        read_as_str = set(re.findall(r'data\.get\("([a-z_]+)",\s*""\)', source))
        # `type` is checked against an allowlist before dispatch; the free-text
        # payloads (text/prompt/message/keys/dir) are sanitised by their own
        # handlers, which must keep doing so.
        exempt = {"type", "text", "prompt", "message", "keys", "dir"}
        unguarded = read_as_str - set(ws_handler._FRAME_STRING_FIELDS) - exempt
        assert not unguarded, (
            f"handlers read {sorted(unguarded)} as strings but the frame gate "
            f"does not cover them"
        )

    def test_the_loop_actually_applies_it(self):
        """A predicate in place but never consulted would read the same."""
        import inspect

        from bridge import ws_handler

        assert _calls(ws_handler.ios_websocket, "_frame_string_fields_ok"), (
            "the receive loop no longer calls _frame_string_fields_ok -- "
            "commented out while debugging?"
        )
        assert "non-string session_id/target" in inspect.getsource(ws_handler.ios_websocket)

    def test_an_unhashable_id_would_still_have_crashed_the_lookup(self):
        """Proves the guard is load-bearing, not belt-and-braces.

        If ``get_session`` ever grows its own type check this test fails and the
        loop guard can be reconsidered -- rather than both quietly existing.
        """
        from bridge.server_state import sessions

        with pytest.raises(TypeError):
            sessions.get_session([])


class TestTheDocumentedKeyListMatchesTheCode:
    """docs/api.md lists the keys delivered while a decision is open.

    That list is what a client builds its key bar from, and it is the kind of
    documentation that rots silently: the set shrank once already when `BTab`
    was removed (shift+tab *approves* an ExitPlanMode plan), and a doc still
    promising it would have told clients to offer a confirmation as navigation.
    """

    @staticmethod
    def _documented() -> set[str]:
        import pathlib
        import re

        doc = pathlib.Path(__file__).resolve().parents[2] / "docs" / "api.md"
        text = doc.read_text()
        # Bounded to the sentence, and the sentence only. A lazy `(.+?)\.\n`
        # with re.S ran past the end of a REWRAPPED list to the next
        # line-final period six lines later, swallowing a clause of prose into
        # the key set -- so an ordinary reflow of docs/api.md turned this into an
        # unreadable failure while the "never promises a confirmation" backstop
        # below went green on the corrupt parse.
        match = re.search(
            r"Still delivered, because none of them can answer:\s*([^.]*?)\.\s",
            text,
        )
        assert match, "docs/api.md no longer lists the keys that stay allowed"
        names = {
            k.strip().strip("`").lower()
            for k in match.group(1).replace("\n", " ").split(",")
        }
        # Every entry must look like a tmux key name, never a clause of prose.
        for name in names:
            assert name and " " not in name, (
                f"parsed {name!r} out of docs/api.md -- the sentence was "
                f"probably rewrapped; fix the regex, do not widen this"
            )
        return names

    def test_the_lists_are_identical(self):
        from bridge.ws_handler import _NON_ANSWERING_KEYS

        assert self._documented() == set(_NON_ANSWERING_KEYS)

    def test_the_docs_do_not_promise_a_confirmation(self):
        """Whatever the list says, these must never be on it."""
        for confirming in ("enter", "return", "tab", "btab", "space", "y"):
            assert confirming not in self._documented()


def _calls(func, name: str) -> bool:
    """True when *func*'s body really contains a call to *name*.

    Parsed, not grepped. A substring search over ``inspect.getsource`` passes on
    a commented-out call -- which is the single commit shape most likely to
    cause the regression ("comment out the gate while debugging the emulator,
    forget to put it back"), and the one these assertions exist to catch.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == name)
             or (isinstance(node.func, ast.Attribute) and node.func.attr == name))
        for node in ast.walk(tree)
    )
