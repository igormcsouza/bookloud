"""Book language detection + per-language TTS voices.

Detection is a stopword vote over a text sample -- no dependency, and good
enough to tell English/Portuguese/French apart (distinct, very frequent
function words). # ponytail: 3 languages only; swap in a real detector
(e.g. lingua) if more are added or mixed-language books matter.
"""

from __future__ import annotations

import re

DEFAULT_LANGUAGE = "en"
SAMPLE_CHARS = 20_000

_STOPWORDS = {
    "en": frozenset("the and of to in is that it was for with as his her they this are but not you have from".split()),
    "pt": frozenset("o a os as de da do das dos e que em um uma para com não nao é se por mais como mas foi ao ele ela você".split()),
    "fr": frozenset("le la les de des du et que en un une est pour dans qui pas sur au avec ce il elle se ne nous vous".split()),
}

# language -> voice. English stays env-configurable via Settings.
EDGE_VOICES = {"en": "en-US-AriaNeural", "pt": "pt-BR-FranciscaNeural", "fr": "fr-FR-DeniseNeural"}
GOOGLE_VOICES = {"en": "en-US-Neural2-C", "pt": "pt-BR-Neural2-A", "fr": "fr-FR-Neural2-A"}


def detect_language(text: str) -> str:
    words = re.findall(r"[^\W\d_]+", text[:SAMPLE_CHARS].lower())
    scores = {lang: sum(w in stops for w in words) for lang, stops in _STOPWORDS.items()}
    best = max(scores, key=scores.__getitem__)
    return best if scores[best] > 0 else DEFAULT_LANGUAGE
