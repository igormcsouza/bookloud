from typing import Any
from src.contexts.library.domain.chat import ChatMessage, ChatRole, FinishReason
from src.contexts.library.infrastructure.keys import pk_book, sk_chat, CHAT_PREFIX

def to_item(msg: ChatMessage) -> dict[str, Any]:
    item = {
        "PK": pk_book(msg.book_id),
        "SK": sk_chat(msg.created_at, msg.message_id),
        "role": msg.role.value,
        "content": msg.content,
        "anchoredChunk": msg.anchored_chunk,
        "createdAt": msg.created_at,
        "userId": msg.user_id,
    }
    if msg.position_ms is not None:
        item["positionMs"] = msg.position_ms
    if msg.model is not None:
        item["model"] = msg.model
    if msg.finish_reason is not None:
        item["finishReason"] = msg.finish_reason.value
    if msg.input_tokens is not None:
        item["inputTokens"] = msg.input_tokens
    if msg.output_tokens is not None:
        item["outputTokens"] = msg.output_tokens
    if msg.cached_input_tokens is not None:
        item["cachedInputTokens"] = msg.cached_input_tokens
    return item

def from_item(item: dict[str, Any]) -> ChatMessage:
    pk = item["PK"]
    sk = item["SK"]
    
    # Extract created_at and msg_id from SK
    # CHAT#<createdAt>#<msgId>
    parts = sk.removeprefix(CHAT_PREFIX).split("#", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid chat SK: {sk}")
        
    created_at, msg_id = parts
    book_id = pk.removeprefix("BOOK#")

    return ChatMessage(
        book_id=book_id,
        message_id=msg_id,
        user_id=item["userId"],
        role=ChatRole(item["role"]),
        content=item["content"],
        anchored_chunk=int(item["anchoredChunk"]),
        created_at=item["createdAt"],
        position_ms=int(item["positionMs"]) if "positionMs" in item else None,
        model=item.get("model"),
        finish_reason=FinishReason(item["finishReason"]) if "finishReason" in item else None,
        input_tokens=int(item["inputTokens"]) if "inputTokens" in item else None,
        output_tokens=int(item["outputTokens"]) if "outputTokens" in item else None,
        cached_input_tokens=int(item["cachedInputTokens"]) if "cachedInputTokens" in item else None,
    )
