"""Primary -> fallback decorator over two ``SpeechSynthesizer``s (PLANS/
phase-4.md §7.7). **The only place that knows edge-tts is primary and
Google is the fallback** -- both adapters are engine-agnostic peers with no
idea the other exists.

**The ``TTS_FALLBACK_TRIGGERED`` token in the warning below is a metric-filter
contract, not incidental phrasing** (``IMPLEMENTATION_PLAN.md`` phase 8;
deferred from ``PLANS/phase-4.md`` OQ-E). ``infra/stacks/pipeline_stack.py``
defines a CloudWatch Logs metric filter over the synthesize Lambda's log
group matching this exact literal, feeding an alarm that fires the moment
Google TTS starts covering for edge-tts even once -- the free-tier-burn risk
OQ-E named. Do not reword this token without updating the metric filter
pattern in lockstep (same "separately deployed projects" rule the ``ENV_*``
constants already carry).
"""

from __future__ import annotations

import logging

from src.contexts.library.domain.synthesis import SpeechSynthesizer, SynthesizedAudio, UnsynthesizableText

logger = logging.getLogger("bookloud.synthesis.fallback")


class FallbackSynthesizer:
    name = "fallback"

    def __init__(self, primary: SpeechSynthesizer, fallback: SpeechSynthesizer) -> None:
        self._primary = primary
        self._fallback = fallback

    def synthesize(self, text: str) -> SynthesizedAudio:
        try:
            return self._primary.synthesize(text)
        except UnsynthesizableText:
            # Permanent: a property of the input (empty/oversized text), not
            # of the engine -- the fallback would fail identically.
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see edge_tts_synthesizer.py
            logger.warning(
                "TTS_FALLBACK_TRIGGERED primary synthesizer %s failed, falling back to %s: %s",
                self._primary.name,
                self._fallback.name,
                exc,
            )
        return self._fallback.synthesize(text)  # its own failures propagate as-is
