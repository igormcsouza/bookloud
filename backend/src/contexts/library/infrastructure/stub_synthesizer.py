"""Returned by ``get_speech_synthesizer()`` whenever ``ENVIRONMENT != "prod"``
(PLANS/phase-4.md §0). Synthesizes nothing and never touches the network --
keeps local dev and every ephemeral PR stack from depending on a live
third-party endpoint. Real edge-tts/Google calls are proven by actually
using the deployed prod app, not by an automated check hitting a paid/
rate-limited third party.
"""

from __future__ import annotations

from src.contexts.library.domain.synthesis import SynthesizedAudio, SynthesisDisabled


class StubSynthesizer:
    name = "stub"

    def synthesize(self, text: str) -> SynthesizedAudio:
        raise SynthesisDisabled()
