"""The seam between the voice engine and a business domain.

The engine — telephony, VAD, STT, TTS, LLM routing, turn-taking, barge-in, the outbound dialer and
the retry outbox — knows nothing about what a call is for. Everything that does lives behind this
interface: the persona and system prompt, the tools the model may call, the opening line for a
call, guardrails applied before and after the model, and what happens once the call ends.

A deployment has exactly one DomainPlugin. `app/domain/real_estate/` is the only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from app.lang.languages import Lang
from app.llm.base import Message, ToolSpec

# Phrase keys the engine itself speaks. Every PhraseBook must render these in every language.
ENGINE_PHRASES: tuple[str, ...] = ("thinking", "silence_prompt", "still_there", "closing", "transfer",
                                   "no_answer")


class PhraseBook(Protocol):
    """Static sentences, pre-rendered to audio at startup so they cost no TTS time mid-call."""

    #: Proper nouns (project, locality, builder names) with their Devanagari spelling.
    names: Mapping[str, str]

    def keys(self) -> tuple[str, ...]: ...
    def render(self, key: str, lang: Lang) -> str: ...


@dataclass
class ToolOutcome:
    """What a tool hands back. `content` is what the model sees; the rest steers the engine."""

    content: dict[str, Any]
    #: End the call once the current reply has been spoken.
    end_call: bool = False
    end_reason: str | None = None
    #: Skip the engine's closing phrase (the model has already said goodbye).
    skip_closing: bool = False


@dataclass
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any]], Awaitable[ToolOutcome]]
    timeout_s: float = 3.0
    #: Phrase key played while the tool runs; None means stay silent.
    filler: str | None = "thinking"


@dataclass
class CallerTurnAction:
    """A domain decision about a caller turn, taken before the model sees it."""

    #: Literal text to speak (already in the caller's language), or a phrase key.
    say: str | None = None
    phrase: str | None = None
    end_call: bool = False
    end_reason: str | None = None
    #: Also let the model answer this turn after speaking.
    continue_to_model: bool = False


@dataclass
class CallInfo:
    call_id: str
    direction: str = "inbound"
    caller_phone: str | None = None
    language_hint: Lang | None = None
    #: Telephony custom parameters (outbound request id, campaign, …). Opaque to the engine.
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallRecord:
    """Everything the engine observed, handed to the domain when the call ends."""

    info: CallInfo
    transcript: list[dict[str, Any]]
    languages_used: list[str]
    final_language: str
    duration_s: float
    end_reason: str
    metrics: dict[str, Any]
    #: Usage and cost so far (app.observability.cost.CostMeter); post-call work adds to it.
    cost: Any = None


class Conversation(Protocol):
    """Per-call domain state. Created once per call by the plugin."""

    #: Language to greet in; read after `start()`.
    initial_language: Lang

    async def start(self) -> None:
        """Resolve who is calling (lead lookup, outbound context). Bounded by the engine."""

    def opening(self, lang: Lang) -> tuple[str, str | None]:
        """(text, phrase_key) for the first thing said. A phrase key uses pre-rendered audio."""

    def messages(self, lang: Lang) -> list[Message]:
        """System messages for the next model call (persona, guardrails, goal, context)."""

    def tools(self) -> list[Tool]: ...

    def screen_caller(self, text: str, lang: Lang) -> CallerTurnAction | None:
        """Inspect a caller turn before the model does; None lets the model answer."""

    def screen_reply(self, sentence: str, lang: Lang) -> str:
        """Inspect a sentence the model produced before it is spoken; returns what to say."""

    def on_agent_reply(self, text: str) -> None: ...

    async def finish(self, record: CallRecord) -> None:
        """Post-call work: extraction, scoring, delivery to the system of record."""


class DomainPlugin(Protocol):
    name: str
    phrases: PhraseBook

    def conversation(self, info: CallInfo) -> Conversation: ...

    def probe_messages(self) -> list[Message]:
        """A representative prompt for the synthetic LLM latency probe."""

    def is_sensitive(self, text: str) -> bool:
        """True when a turn names data that must be redacted from logs and transcripts."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def health(self) -> dict[str, Any]: ...
