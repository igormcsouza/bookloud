import re
from typing import Iterator

from src.contexts.library.domain.chat import ChatContext, ChatDelta, ChatDisabledReason


PROD_NOT_CONFIGURED_MESSAGE = (
    "Chat isn't set up for this deployment yet — no language-model key is configured. "
    "You can keep reading and listening; ask again once a key is added."
)


def _chunked(text: str) -> Iterator[ChatDelta]:
    """Splits on word boundaries and yields ~6 words at a time."""
    # Split by spaces but keep the spaces so we can reconstruct
    tokens = re.split(r'(\s+)', text)
    
    current_chunk = ""
    word_count = 0
    
    for token in tokens:
        current_chunk += token
        if not token.isspace():
            word_count += 1
            
        if word_count >= 6:
            yield ChatDelta(text=current_chunk)
            current_chunk = ""
            word_count = 0
            
    if current_chunk:
        yield ChatDelta(text=current_chunk)


class StubChatModel:
    """Returned by get_chat_model() whenever ENVIRONMENT != "prod", and in
    prod whenever OPENAI_SECRET_NAME is "" (its default everywhere).
    """
    name = "stub"
    enabled = False

    def __init__(self, reason: ChatDisabledReason) -> None:
        self.reason = reason
        self._reason = reason

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]:
        if self._reason is ChatDisabledReason.NOT_CONFIGURED:
            yield from _chunked(PROD_NOT_CONFIGURED_MESSAGE)
            return
            
        anchor = context.anchor
        msg = (
            f"[Chat is turned off in this environment.] "
            f"You asked: {context.question} "
            f"I can see {len(context.chunks)} sections of "
            f"“{context.book_title}” around section {anchor.index}, "
            f"which begins: “{anchor.text[:80].strip()}…”"
        )
        yield from _chunked(msg)
