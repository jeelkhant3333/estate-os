"""Any OpenAI-compatible chat completions API (Gnani Evon for the Gnani experiment).

Same streaming and tool-call handling as the Sarvam adapter; only the URL, the auth header and the
request body differ (no Sarvam-specific reasoning_effort field).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import Message, ToolSpec
from app.llm.sarvam import SarvamLLM


class OpenAICompatLLM(SarvamLLM):
    def __init__(self, name: str, api_key: str, model: str, base_url: str, auth_header: str = "Authorization",
                 client: httpx.AsyncClient | None = None, timeout_s: float = 30.0):
        super().__init__(api_key, model, base_url, None, client, timeout_s)
        self.name = name
        base = base_url.rstrip("/")
        self._url = base if base.endswith("/chat/completions") else base + "/v1/chat/completions"
        value = f"Bearer {api_key}" if auth_header.lower() == "authorization" else api_key
        self._headers = {auth_header: value, "Content-Type": "application/json"}

    def payload(self, messages: list[Message], tools: list[ToolSpec], max_tokens: int, temperature: float) -> dict[str, Any]:
        body = super().payload(messages, tools, max_tokens, temperature)
        body.pop("reasoning_effort", None)
        return body
