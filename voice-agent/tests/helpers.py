"""Test doubles shared by the engine tests: a domain that does nothing surprising."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.conversation.session import CallSession, SessionDeps
from app.conversation.speech import TextSpeechOutput
from app.domain.base import CallerTurnAction, CallInfo, CallRecord, Tool
from app.llm.base import Message
from app.llm.router import LLMRouter

PHRASES = {
    "greeting": "Hello, how can I help?",
    "thinking": "One moment, let me check.",
    "silence_prompt": "I could not hear you.",
    "still_there": "Are you still there?",
    "closing": "Goodbye.",
    "transfer": "Someone will call you back.",
    "no_answer": "Our expert will confirm that.",
    "filler_lookup": "Let me look that up.",
}


class StubPhrases:
    names: dict[str, str] = {}

    def keys(self) -> tuple[str, ...]:
        return tuple(PHRASES)

    def render(self, key: str, lang: str) -> str:
        return PHRASES[key]


@dataclass
class StubConversation:
    tool_list: list[Tool] = field(default_factory=list)
    screen: Any = None
    initial_language: str = "en"
    replies: list[str] = field(default_factory=list)
    finished: list[CallRecord] = field(default_factory=list)
    screened_replies: Any = None

    async def start(self) -> None:
        return None

    def opening(self, lang):
        return PHRASES["greeting"], "greeting"

    def messages(self, lang) -> list[Message]:
        return [Message("system", "You are a test assistant.")]

    def tools(self) -> list[Tool]:
        return self.tool_list

    def screen_caller(self, text, lang) -> CallerTurnAction | None:
        return self.screen(text) if self.screen else None

    def screen_reply(self, sentence, lang) -> str:
        return self.screened_replies(sentence) if self.screened_replies else sentence

    def on_agent_reply(self, text) -> None:
        self.replies.append(text)

    async def finish(self, record: CallRecord) -> None:
        self.finished.append(record)


def session(llm, conversation: StubConversation | None = None, **deps) -> tuple[CallSession, TextSpeechOutput]:
    options = dict(router=LLMRouter(llm, None), phrases=StubPhrases(), default_language="en",
                   devanagari_speech=False)
    options.update(deps)
    speech = TextSpeechOutput()
    s = CallSession(SessionDeps(**options), CallInfo("t1"), caller=None, speech=speech,
                    hangup=lambda: None, conversation=conversation or StubConversation())
    return s, speech
