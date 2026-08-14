from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.contexts.library.application.chat import AskBookQuestion
from src.contexts.library.domain.chat import ChatModel, FinishReason
from src.contexts.library.interface.dependencies import get_chat_model
from src.contexts.library.interface.schemas import sse_frame
from src.shared_kernel.domain.errors import ChatStreamError
from src.auth.dependencies import get_current_user

# Note: In phase-7, we must wire the dependencies. AskBookQuestion isn't added yet, we will import a builder from dependencies
from src.contexts.library.interface.dependencies import get_ask_book_question, get_chat_repository


class ChatQuestionBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    anchoredChunk: int | None = None


chat_router = APIRouter(tags=["chat"])


@chat_router.post("/books/{book_id}/chat")
async def ask_question(
    book_id: str,
    body: ChatQuestionBody,
    user_id: str = Depends(get_current_user),
    model: ChatModel = Depends(get_chat_model),
    use_case: AskBookQuestion = Depends(get_ask_book_question),
    chat_repository = Depends(get_chat_repository),
):
    # Execute the use case, which checks quota, loads book, and resolves context
    result = use_case.execute(
        user_id=user_id,
        book_id=book_id,
        question=body.question,
        anchor_chunk=body.anchoredChunk,
    )
    
    context = result.context
    user_message = result.user_message
    assistant_message = result.assistant_message

    async def _body() -> AsyncIterator[bytes]:
        meta = {
            "enabled": True,
            "reason": None,
            "model": model.name,
            "anchorChunk": context.anchor_chunk,
            "windowChunks": list(context.window_indexes),
            "messageId": assistant_message.message_id,
        }
        yield sse_frame("meta", meta)
        
        answer_text = []
        finish = FinishReason.END_TURN
        
        try:
            for delta in model.stream(context):
                answer_text.append(delta.text)
                yield sse_frame("delta", {"text": delta.text})
        except ChatStreamError as exc:
            finish = FinishReason.ERROR
            yield sse_frame("error", {"code": exc.code, "message": exc.public_message})
        finally:
            if answer_text:
                assistant_message.content = "".join(answer_text)
                assistant_message.finish_reason = finish
                chat_repository.save_turn(user_message, assistant_message)
                
            yield sse_frame("done", {"finishReason": finish.value, "messageId": assistant_message.message_id})

    return StreamingResponse(_body(), media_type="text/event-stream")
