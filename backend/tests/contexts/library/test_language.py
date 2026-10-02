import pytest

from src.contexts.library.domain.language import DEFAULT_LANGUAGE, EDGE_VOICES, GOOGLE_VOICES, detect_language
from src.contexts.library.infrastructure.edge_tts_synthesizer import EdgeTtsSynthesizer
from tests.contexts.library.fakes import FakeCommunicateFactory
from tests.contexts.library.test_edge_tts_synthesizer import _TEXT, _word_boundary_chunks


@pytest.mark.parametrize(
    "text, expected",
    [
        ("The man was in the house and they said that it was for his wife.", "en"),
        ("O homem n\u00e3o estava em casa e ela disse que era para voc\u00ea.", "pt"),
        ("Il n'est pas dans la maison et elle dit que c'est pour nous.", "fr"),
        ("O HOMEM N\u00c3O ESTAVA EM CASA E ELA DISSE QUE ERA PARA VOC\u00ca.", "pt"),  # case-insensitive
        ("", DEFAULT_LANGUAGE),
        ("1234 !!! ???", DEFAULT_LANGUAGE),
        ("Xyzzy plugh quux", DEFAULT_LANGUAGE),  # no stopword hit -> default
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def test_detect_language_only_samples_the_start():
    # 30k chars of English followed by a lot of Portuguese: sample window decides.
    text = ("the and of to in is that it was " * 1000) + ("o a de que n\u00e3o " * 5000)
    assert detect_language(text) == "en"


def test_every_language_has_both_voices():
    assert set(EDGE_VOICES) == set(GOOGLE_VOICES)


@pytest.mark.parametrize(
    "language, voice",
    [("pt", EDGE_VOICES["pt"]), ("fr", EDGE_VOICES["fr"]), ("xx", "custom-voice")],  # unknown -> configured voice
)
def test_edge_picks_voice_by_language(language, voice):
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    result = EdgeTtsSynthesizer(voice="custom-voice", communicate_cls=factory, attempts=1).synthesize(_TEXT, language)
    assert factory.instances[0].voice == voice
    assert result.voice == voice


def test_edge_english_uses_configured_voice():
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    EdgeTtsSynthesizer(voice="en-GB-SoniaNeural", communicate_cls=factory, attempts=1).synthesize(_TEXT, "en")
    assert factory.instances[0].voice == "en-GB-SoniaNeural"
