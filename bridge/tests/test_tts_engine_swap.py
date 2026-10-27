"""``--openai-tts`` replaces the speech engine; every caller must see the swap.

``cli.py`` rebinds ``bridge.server_state.mac_tts`` to an ``OpenAITTS`` *after*
the route module has been imported. ``routes.py`` had taken the object by value
(``from bridge.server_state import ... mac_tts ...``), so it kept speaking to
the original ``MacTTS`` -- which the lifespan never calls ``start()`` on, so its
queue stays ``None`` and ``speak()`` returns at its first line.

The result was worse than losing the better voice: with ``--openai-tts`` every
``POST /api/tts`` was dropped silently, and ``/api/tts/state`` read and muted an
object nothing was speaking through -- so the mute control reported success and
did nothing. Nothing spoken, nothing logged, no error.

This is the same class of bug as the module-import ``asyncio.Queue`` in
CLAUDE.md's constraints: state captured before the thing that owns it is built.
"""

from __future__ import annotations

import pytest

import bridge.routes as routes
import bridge.server_state as server_state


class FakeEngine:
    """Stands in for OpenAITTS: same surface, records what it was told."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = True
        self.available = True
        self.spoken: list[str] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def speak(self, text: str, **_kwargs) -> None:
        self.spoken.append(text)


@pytest.fixture
def swapped():
    """Swap the engine the way ``--openai-tts`` does, then put it back."""
    original = server_state.mac_tts
    engine = FakeEngine("openai")
    server_state.mac_tts = engine
    try:
        yield engine
    finally:
        server_state.mac_tts = original


class TestTheSwapIsSeen:
    def test_the_route_module_holds_no_engine_of_its_own(self):
        """A module-level name here would freeze whichever engine was first."""
        assert not hasattr(routes, "mac_tts"), (
            "routes.mac_tts is an import-time binding; --openai-tts rebinds "
            "bridge.server_state.mac_tts and this copy would not follow"
        )

    async def test_speech_reaches_the_swapped_engine(self, swapped):
        result = await routes.tts_speak({"text": "hello from the phone"})
        assert swapped.spoken == ["hello from the phone"]
        assert result.get("ok") or "error" not in result

    async def test_the_mute_toggle_reaches_the_swapped_engine(self, swapped):
        await routes.set_tts_state({"enabled": False})
        assert swapped.enabled is False, "mute set the flag on a stale object"

    async def test_the_reported_state_is_the_swapped_engines(self, swapped):
        swapped.enabled = False
        swapped.available = True
        state = await routes.tts_state()
        assert state == {"enabled": False, "available": True}

    async def test_the_registration_announcement_reaches_it_too(
        self, swapped, monkeypatch, tmp_path
    ):
        """Behavioural: actually register a session and listen for the line.

        This used to grep ``inspect.getsource(routes)`` for a bare ``mac_tts.``,
        which the documented bug defeats trivially: reintroduce it as
        ``from bridge.server_state import mac_tts as _tts`` and the token is
        gone from the source while the stale object is back. It also did not
        call ``register_session`` at all, so it tested nothing of what it named.
        """
        import os

        from bridge.server_state import sessions

        project_dir = os.path.join(
            os.path.expanduser("~"), ".voxherd-test-projects", "tts-swap"
        )
        os.makedirs(project_dir, exist_ok=True)

        class LoopbackRequest:
            client = type("C", (), {"host": "127.0.0.1"})()
            headers: dict = {}

        try:
            result = await routes.register_session(
                LoopbackRequest(),
                {
                    "session_id": "tts-swap-sid",
                    "project": "ttsswap",
                    "project_dir": project_dir,
                    "tmux_target": "ttsswap:0.0",
                },
            )
            assert "error" not in result, result
            assert any("registered" in line for line in swapped.spoken), (
                f"the registration announcement did not reach the swapped "
                f"engine; it said {swapped.spoken!r}"
            )
        finally:
            sessions.remove_session("tts-swap-sid")

    def test_no_bare_engine_reference_survives_in_the_route_module(self):
        """Belt to the braces above, and honest about what it is.

        A source scan, so an aliased import defeats it -- which is why the test
        above is behavioural. This one catches the ordinary shape: someone
        reintroducing ``mac_tts.speak(...)`` at a NEW call site that no test
        exercises yet.
        """
        import inspect

        source = inspect.getsource(routes)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "mac_tts." not in stripped.replace("_state.mac_tts.", ""), \
                f"a bare mac_tts reference survives: {stripped!r}"


class TestTheLifespanStartsTheLiveEngine:
    def test_start_is_called_on_the_module_attribute(self):
        """``_state.mac_tts.start()``, not a captured copy.

        If the lifespan started a copy instead, the swapped-in engine would
        never get its queue and the symptom would simply move.
        """
        import inspect

        import bridge.bridge_server as bridge_server

        source = inspect.getsource(bridge_server)
        assert "_state.mac_tts.start()" in source
