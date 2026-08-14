import json
import urllib.request
from typing import Iterator

from src.contexts.library.domain.chat import ChatContext, ChatDelta, ChatModel
from src.contexts.library.infrastructure.openai_prompt import render_messages
from src.contexts.library.infrastructure.secrets import get_secret
from src.shared_kernel.domain.errors import ChatStreamError

class OpenAiChatModel(ChatModel):
    def __init__(self, secret_name: str, model: str, max_output_tokens: int) -> None:
        self.secret_name = secret_name
        self.name = model
        self.max_output_tokens = max_output_tokens

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]:
        api_key = get_secret(self.secret_name)
        messages = render_messages(context)
        
        payload = {
            "model": self.name,
            "messages": messages,
            "max_completion_tokens": self.max_output_tokens,
            "stream": True,
        }
        
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            },
            method="POST"
        )
        
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            raise ChatStreamError("Failed to connect to model", "CONNECT_ERROR") from e
            
        try:
            with response:
                for line in response:
                    line = line.strip()
                    if not line:
                        continue
                        
                    if line.startswith(b"data: "):
                        data_str = line[6:].decode("utf-8")
                        if data_str == "[DONE]":
                            break
                            
                        data = json.loads(data_str)
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if "content" in delta:
                                yield ChatDelta(text=delta["content"])
        except Exception as e:
            raise ChatStreamError("Network failure mid-stream", "STREAM_ERROR") from e
