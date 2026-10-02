import json

import pytest

from src.contexts.library.infrastructure.openai_title_inferrer import (
    MAX_INPUT_CHARS,
    OpenAiTitleInferrer,
    parse_book_title,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"bookTitle": "The adventures of using AI"}', "The adventures of using AI"),
        ('```json\n{"bookTitle": "  Spaced \\n Out  "}\n```', "Spaced Out"),
        ('Sure! Here you go: {"bookTitle": "Chatty"} Hope it helps.', "Chatty"),
        ('{"bookTitle": null}', None),
        ('{"bookTitle": "   "}', None),
        ('{"bookTitle": 42}', None),
        ('{"other": "x"}', None),
        ("[1, 2]", None),
        ("not json", None),
        ("{broken", None),
        ("", None),
    ],
)
def test_parse_book_title(raw: str, expected: str | None) -> None:
    assert parse_book_title(raw) == expected


def test_parse_book_title_caps_length() -> None:
    assert len(parse_book_title(json.dumps({"bookTitle": "x" * 500}))) == 200


class _Response:
    def __init__(self, content: str) -> None:
        self._body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_infer_sends_only_the_opening_text_and_parses_reply() -> None:
    sent = {}

    def http(request, timeout):
        sent["payload"] = json.loads(request.data)
        sent["auth"] = request.get_header("Authorization")
        return _Response('{"bookTitle": "Dune"}')

    inferrer = OpenAiTitleInferrer(api_key="sk-test", model="m", http=http)

    assert inferrer.infer("x" * 10_000) == "Dune"
    assert len(sent["payload"]["messages"][1]["content"]) == MAX_INPUT_CHARS
    assert sent["payload"]["response_format"] == {"type": "json_object"}
    assert sent["auth"] == "Bearer sk-test"
    assert "sk-test" not in repr(inferrer)
