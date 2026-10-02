"""``TitleInferrer`` over OpenAI's Chat Completions API (JSON mode).

Same shape as ``openai_chat_model.py``: plain ``urllib`` with an injected
HTTP callable, api_key held only on the instance, never logged. Only the
first ``MAX_INPUT_CHARS`` of the book are ever sent. No retry: a miss just
leaves the filename title in place, so extraction is never blocked on it.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MAX_INPUT_CHARS = 3000
MAX_TITLE_CHARS = 200
_TIMEOUT_SECONDS = 15.0

_SYSTEM_PROMPT = (
    "You are given the opening text of a document (a book, article or paper). "
    "Identify its title as printed on the title page or first page. Reply with "
    'only a JSON object: {"bookTitle": "<title>"}. Use proper capitalisation; '
    'no author name, no file extension. If you cannot tell, reply {"bookTitle": null}.'
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def parse_book_title(raw: str) -> str | None:
    """Pull ``bookTitle`` out of an LLM reply. Tolerates markdown fences and
    chatter around the JSON; returns ``None`` for anything unusable."""
    match = _JSON_OBJECT_RE.search(raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    title = data.get("bookTitle") if isinstance(data, dict) else None
    if not isinstance(title, str):
        return None
    title = _WHITESPACE_RE.sub(" ", title).strip()
    return title[:MAX_TITLE_CHARS].strip() or None


def _default_http(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 -- fixed https endpoint


class OpenAiTitleInferrer:
    def __init__(self, *, api_key: str, model: str, http: Callable[..., Any] | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._http = http if http is not None else _default_http

    def __repr__(self) -> str:
        return f"OpenAiTitleInferrer(model={self._model!r})"

    __str__ = __repr__

    def infer(self, text: str) -> str | None:
        payload = {
            "model": self._model,
            "temperature": 0,
            "max_completion_tokens": 80,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text[:MAX_INPUT_CHARS]},
            ],
        }
        request = urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        with self._http(request, _TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
        return parse_book_title(body["choices"][0]["message"]["content"])
