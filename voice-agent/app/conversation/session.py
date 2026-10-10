"""One phone conversation with a caller.

Domain-neutral: the persona, the tools, the guardrails and the post-call work come from a
`Conversation` supplied by the domain plugin (app/domain/base.py). This module owns turn-taking,
barge-in, silence handling, sentence-by-sentence streaming to TTS, and the tool-calling loop:
the model may call tools, their results go back to the model, and only then does it answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable

from app.conversation.events import (FinalTranscript, PartialTranscript, SpeechEnded, SpeechStarted,
                                     STTFailure)
from app.conversation.interruption import BargeIn, BargeInDetector, is_backchannel, is_hesitation
from app.conversation.silence import SilenceWatchdog
from app.conversation.turn_detection import TurnConfig, TurnDetector
from app.domain.base import (CallerTurnAction, CallInfo, CallRecord, Conversation, PhraseBook, Tool,
                             ToolOutcome)
from app.lang.devanagari import to_devanagari_speech
from app.lang.spoken import crore_not_lakh
from app.lang.languages import Lang, LanguageTracker, is_noise, to_devanagari_script
from app.lang.redaction import redact
from app.llm.base import Message, TextDelta, ToolCall, ToolCallReady, Usage
from app.observability.cost import CostMeter
from app.llm.router import AllProvidersFailed, LLMRoute, LLMRouter

log = logging.getLogger(__name__)

# A sentence ends at a full stop, danda, question or exclamation mark followed by a space, so
# "7.25%" is never split.
SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")
# The first piece may also end at a comma once it is this long, so a long opening sentence does
# not hold back the first audio. "1,00,000" has no space after its commas and is never split.
CLAUSE_END = re.compile(r"(?<=,)\s+")
FIRST_CLAUSE_CHARS = 50
MIN_SENTENCE_CHARS = 12
_PHRASE = object()

GOODBYE = ("bye", "goodbye", "thank you", "thanks", "thats all", "no thanks",
           "धन्यवाद", "शुक्रिया", "बस", "नको", "काही नाही", "थँक्यू", "આભાર", "બસ")

__all__ = ["CallInfo", "CallSession", "SessionDeps", "CallMetrics"]


@dataclass
class SessionDeps:
    router: LLMRouter
    phrases: PhraseBook
    default_language: Lang = "hi"
    language_switch_confidence: float = 0.6
    llm_max_tokens: int = 220
    llm_temperature: float = 0.2
    devanagari_speech: bool = True
    turn_config: TurnConfig = field(default_factory=TurnConfig)
    silence_first_prompt_s: float = 6.0
    silence_interval_s: float = 5.0
    barge_in_min_speech_ms: int = 300
    # Say the "one moment" filler only when the first sentence of the answer is this late (0 = never).
    filler_after_ms: int = 1200
    # Model calls per caller turn: tool hops plus the final answer.
    max_tool_hops: int = 3
    # Whole earlier turns (with their tool calls) replayed to the model.
    history_turns: int = 4
    # The domain resolves the caller (lead lookup) before the greeting; never longer than this.
    start_timeout_s: float = 1.5
    sensitive: Callable[[str], bool] | None = None


@dataclass
class CallMetrics:
    started_at: float = field(default_factory=time.monotonic)
    replies: list[float] = field(default_factory=list)
    interruptions: int = 0
    unanswered: int = 0
    screened: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    end_reason: str = "completed"

    @property
    def duration_s(self) -> float:
        return time.monotonic() - self.started_at

    def summary(self) -> dict:
        ordered = sorted(self.replies)
        return {"duration_s": round(self.duration_s, 2), "turns": len(self.replies),
                "p50_reply_ms": round(ordered[len(ordered) // 2] * 1000) if ordered else None,
                "interruptions": self.interruptions, "unanswered": self.unanswered,
                "screened": self.screened, "tool_calls": self.tool_calls,
                "tool_errors": self.tool_errors, "end_reason": self.end_reason}


class CallSession:
    def __init__(self, deps: SessionDeps, info: CallInfo, caller, speech,
                 hangup: Callable[[], object], conversation: Conversation):
        self.deps = deps
        self.info = info
        self.caller = caller
        self.speech = speech
        self.hangup = hangup
        self.conversation = conversation
        self.lang = LanguageTracker(info.language_hint or deps.default_language,
                                    switch_confidence=deps.language_switch_confidence)
        self.metrics = CallMetrics()
        self.transcript: list[dict] = []
        self.turns = TurnDetector(deps.turn_config)
        self.barge = BargeInDetector(min_speech_ms=deps.barge_in_min_speech_ms)
        self.silence = SilenceWatchdog(deps.silence_first_prompt_s, deps.silence_interval_s,
                                       self._on_silence, self._on_silence_timeout)
        self.route = LLMRoute()
        # Earlier turns, each the full message sequence (user, tool calls, tool results, answer).
        self._exchanges: list[list[Message]] = []
        self._ending = False
        self._pending_end: ToolOutcome | None = None
        self._goodbye_said = False
        self._variant_turns: dict[str, int] = {}
        self.cost = CostMeter()
        self._tasks: list[asyncio.Task] = []
        self._reply: asyncio.Task | None = None
        self._barge_task: asyncio.Task | None = None

    # ---- speech out

    def _spoken(self, text: str, lang: Lang) -> str:
        text = crore_not_lakh(text, lang)
        return (to_devanagari_speech(text, lang, self.deps.phrases.names)
                if self.deps.devanagari_speech else text)

    async def _say(self, text: str, lang: Lang, phrase_key: str | None = None) -> None:
        self.transcript.append({"speaker": "agent", "text": text})
        await self.speech.speak_text(self._spoken(text, lang), lang, phrase_key=phrase_key)

    async def _say_phrase(self, key: str, lang: Lang) -> None:
        key = self._next_variant(key)
        await self._say(self.deps.phrases.render(key, lang), lang, phrase_key=key)

    def _next_variant(self, key: str) -> str:
        """Rotate through a phrase's versions within the call: "let me check" in a different wording each time."""
        variants = getattr(self.deps.phrases, "variants", None)
        options = variants(key) if variants else (key,)
        n = self._variant_turns.get(key, 0)
        self._variant_turns[key] = n + 1
        return options[n % len(options)]

    async def _on_silence(self, count: int) -> None:
        """Nudge a quiet caller; the watchdog gives up after its configured number of prompts.

        The watchdog paces its own prompts, so this must not re-arm it: a second timer would
        survive the caller speaking and keep prompting over the conversation.
        """
        if self._ending or self.speech.speaking or (self._reply and not self._reply.done()):
            return
        # The caller is mid-sentence (voice detected, or words still arriving): not silence.
        if self.turns.speaking or self.turns.current_text():
            return
        # The first quiet spell gets no line: it usually follows a sound the agent ignored as noise
        # or a "hmm", and "sorry, I couldn't hear you" to a caller who just spoke sounds broken.
        if count == 1:
            log.info("call %s: caller quiet, waiting before a prompt", self.info.call_id)
            return
        log.info("call %s: silence prompt %d", self.info.call_id, count)
        await self._say_phrase("still_there", self.lang.current)

    async def _on_silence_timeout(self) -> None:
        if self._ending:
            return
        self.metrics.end_reason = "caller_silent"
        await self._end()

    # ---- answering

    def _history(self) -> list[Message]:
        """Recent exchanges for the model. Only the latest keeps its tool calls and results (a visit
        is booked with the slot times offered one turn earlier); older ones keep just what was said,
        so earlier lookups do not grow every later prompt and delay the answer."""
        recent = self._exchanges[-self.deps.history_turns:]
        out: list[Message] = []
        for i, turn in enumerate(recent):
            if i == len(recent) - 1:
                out.extend(turn)
            else:
                out.extend(m for m in turn if m.role in ("user", "assistant") and not m.tool_calls)
        return out

    async def _run_tool(self, tools: dict[str, Tool], call: ToolCall) -> ToolOutcome:
        self.metrics.tool_calls += 1
        tool = tools.get(call.name)
        if tool is None:
            self.metrics.tool_errors += 1
            return ToolOutcome({"error": "unknown_tool", "tool": call.name})
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError:
            self.metrics.tool_errors += 1
            return ToolOutcome({"error": "invalid_arguments"})
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(tool.run(args), tool.timeout_s)
        except asyncio.TimeoutError:
            self.metrics.tool_errors += 1
            log.warning("call %s: tool %s timed out after %.1fs", self.info.call_id, call.name,
                        tool.timeout_s)
            return ToolOutcome({"error": "timeout",
                                "instruction": "Say you will have this confirmed; do not guess."})
        except Exception:  # noqa: BLE001 - a broken tool must never end the call
            self.metrics.tool_errors += 1
            log.exception("call %s: tool %s failed", self.info.call_id, call.name)
            return ToolOutcome({"error": "failed",
                                "instruction": "Say you will have this confirmed; do not guess."})
        log.info("call %s: tool %s in %.0fms", self.info.call_id, call.name,
                 (time.monotonic() - started) * 1000)
        return outcome

    async def answer_sentences(self, question: str, lang: Lang):
        """Stream the answer, one sentence at a time.

        Between model calls the requested tools run. While they do, a `(_PHRASE, key)` marker is
        yielded so the caller hears the tool's filler instead of silence.
        """
        tools = {t.spec.name: t for t in self.conversation.tools()}
        specs = [t.spec for t in tools.values()]
        working: list[Message] = [Message("user", question)]
        reply = ""
        ending = False
        filler_said = False  # one "let me check" per turn, however many lookups it takes
        try:
            for hop in range(self.deps.max_tool_hops + 1):
                last_hop = hop == self.deps.max_tool_hops

                def build(_provider: str) -> list[Message]:
                    return self.conversation.messages(lang) + self._history() + working

                buffer, hop_text, calls = "", "", []
                hop_started, first_event_ms = time.monotonic(), None
                self.cost.add_request("call")
                # Tools stay declared on the last hop: the conversation already holds tool calls,
                # and a provider rejects that history without tool definitions (Sarvam: 400). Calls
                # made on the last hop are ignored below.
                async for event in self.deps.router.stream(
                        self.route, build, specs,
                        max_tokens=self.deps.llm_max_tokens,
                        temperature=self.deps.llm_temperature):
                    if first_event_ms is None:
                        first_event_ms = (time.monotonic() - hop_started) * 1000
                    if isinstance(event, Usage):
                        self.cost.add_usage("call", event)
                        continue
                    if isinstance(event, ToolCallReady):
                        calls.append(event.call)
                        continue
                    if not isinstance(event, TextDelta):
                        continue
                    buffer += event.text
                    hop_text += event.text
                    parts = SENTENCE_END.split(buffer)
                    if not reply and len(parts) == 1 and len(buffer) >= FIRST_CLAUSE_CHARS:
                        head, _, tail = buffer.rpartition(", ") if CLAUSE_END.search(buffer) else ("", "", buffer)
                        if len(head) >= MIN_SENTENCE_CHARS:
                            parts = [head + ",", tail]
                    ready, buffer = parts[:-1], parts[-1]
                    pending = ""
                    for part in ready:
                        pending = f"{pending} {part}".strip()
                        if len(pending) >= MIN_SENTENCE_CHARS:
                            sentence = self.conversation.screen_reply(pending, lang)
                            pending = ""
                            if not sentence:  # dropped: a repeat, or a visit question already asked
                                continue
                            reply = f"{reply} {sentence}".strip()
                            yield sentence
                    buffer = f"{pending} {buffer}".strip() if pending else buffer
                if buffer.strip():
                    sentence = self.conversation.screen_reply(buffer.strip(), lang)
                    if sentence:
                        reply = f"{reply} {sentence}".strip()
                        yield sentence
                log.info("call %s: model hop %d first output %.0fms, done %.0fms, %d tool call(s)",
                         self.info.call_id, hop, first_event_ms or 0, (time.monotonic() - hop_started) * 1000,
                         len(calls))
                if ending:
                    self._goodbye_said = bool(hop_text.strip())
                    break
                if not calls or last_hop:
                    if not reply:
                        fallback = getattr(self.conversation, "take_dropped", lambda: "")()
                        if fallback:
                            reply = fallback
                            yield fallback
                    break
                working.append(Message("assistant", hop_text, tool_calls=calls))
                filler = next((tools[c.name].filler for c in calls
                               if c.name in tools and tools[c.name].filler), None)
                if filler and not hop_text.strip() and not filler_said and not reply:
                    filler_said = True
                    yield (_PHRASE, filler)
                outcomes = await asyncio.gather(*(self._run_tool(tools, c) for c in calls))
                for call, outcome in zip(calls, outcomes):
                    working.append(Message("tool", json.dumps(outcome.content, ensure_ascii=False,
                                                              default=str),
                                           tool_call_id=call.id, name=call.name))
                    if outcome.end_call and self._pending_end is None:
                        self._pending_end = outcome
                if self._pending_end is not None:
                    # One more hop lets the model say its own goodbye; any tool it calls then
                    # (typically end_call again) is ignored.
                    ending = True
        finally:
            if reply:
                working.append(Message("assistant", reply))
                self.transcript.append({"speaker": "agent", "text": crore_not_lakh(reply, lang)})
                self.conversation.on_agent_reply(reply)
            self._exchanges.append(working)

    async def answer(self, question: str, lang: Lang) -> str:
        """The whole answer as one string (for tests and admin tools; calls stream sentences)."""
        try:
            parts = [s async for s in self.answer_sentences(question, lang)
                     if not isinstance(s, tuple)]
        except AllProvidersFailed:
            return self.deps.phrases.render("transfer", lang)
        return " ".join(parts)

    # ---- loops

    async def _event_loop(self) -> None:
        while True:
            event = await self.caller.events.get()
            if self._ending:
                continue
            if isinstance(event, SpeechStarted):
                self.silence.disarm()
                self.turns.on_speech_start(event.t)
                if self.speech.speaking:
                    self.barge.on_onset(event.t)
                    if self._barge_task is None or self._barge_task.done():
                        self._barge_task = asyncio.create_task(self._watch_barge_in())
            elif isinstance(event, PartialTranscript):
                text = "" if is_noise(event.text) else to_devanagari_script(unicodedata.normalize("NFC", event.text))
                self.turns.on_partial(text)
                self.barge.on_text(text)
                self._words_while_speaking(text)
            elif isinstance(event, FinalTranscript):
                # Hindi spelt in another Indic script is converted, not lost; a jumble of scripts is noise.
                text = "" if is_noise(event.text) else to_devanagari_script(unicodedata.normalize("NFC", event.text))
                self.turns.on_final(text, event.language)
                self.barge.on_text(text)
                self._words_while_speaking(text)
            elif isinstance(event, SpeechEnded):
                self.turns.on_speech_end(event.t)
                self.barge.on_speech_end(event.t)
            elif isinstance(event, STTFailure):
                log.warning("call %s: speech recognition failed: %s", self.info.call_id, event.reason)
                await self.caller.switch_to_fallback()

    def _words_while_speaking(self, text: str) -> None:
        """Never talk over the caller. The recogniser hears every frame, also while Riya speaks; real words
        from the caller stop her at once, even when the voice detector (its threshold raised against her
        own echo) missed the onset. Backchannels ("हाँ", "ok") do not."""
        if not text or not self.speech.speaking or is_backchannel(text):
            return
        if self._barge_task is not None and not self._barge_task.done():
            return  # already deciding on this interruption; it has the text
        self.barge.on_onset(asyncio.get_running_loop().time())
        self.barge.on_text(text)
        self._barge_task = asyncio.create_task(self._watch_barge_in())

    async def _watch_barge_in(self) -> None:
        while True:
            decision = self.barge.evaluate(asyncio.get_running_loop().time())
            if decision == BargeIn.INTERRUPT:
                await self._interrupt()
                return
            if decision == BargeIn.NONE or not self.speech.speaking:
                return
            await asyncio.sleep(0.02)

    async def _interrupt(self) -> None:
        """The caller started talking over Riya: stop speaking and drop the answer in flight."""
        if self._ending or not self.speech.speaking:
            return
        self.metrics.interruptions += 1
        log.info("call %s: caller interrupted (%r)", self.info.call_id,
                 redact(self.barge.text, self.deps.sensitive)[:60])
        await self.speech.stop()
        if self._reply and not self._reply.done():
            self._reply.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._reply

    async def _screened(self, action: CallerTurnAction, lang: Lang) -> bool:
        """Carry out a domain decision about a caller turn. True when the model should not answer."""
        self.metrics.screened += 1
        if action.phrase:
            await self._say_phrase(action.phrase, lang)
        elif action.say:
            await self._say(action.say, lang)
        if action.end_call:
            self.metrics.end_reason = action.end_reason or "domain_end"
            await self._end(say_closing=False)
            return True
        return not action.continue_to_model

    async def _turn_loop(self) -> None:
        while True:
            turn = await self.turns.turns.get()
            if self._ending:
                continue
            text = (turn.text or "").strip()
            if not text:
                continue
            if is_noise(text):
                # Background noise, not the caller: no reply ("sorry, I didn't catch that" to a cough
                # sounds distracted). The silence prompt still covers a caller who has gone quiet.
                log.info("call %s: ignored a noise transcript (%d chars)", self.info.call_id, len(text))
                self.silence.arm()
                continue
            if is_hesitation(text):
                # "हम", "उम्": the caller is thinking. Replying ("sorry, the line broke") interrupts them.
                log.info("call %s: waited through a hesitation", self.info.call_id)
                self.silence.arm()
                continue
            lang = self.lang.observe(text, turn.language)
            self.transcript.append({"speaker": "caller", "text": redact(text, self.deps.sensitive)})

            action = self.conversation.screen_caller(text, lang)
            if action is not None and await self._screened(action, lang):
                if self._ending:
                    return
                self.silence.arm()
                continue

            if len(text.split()) <= 4 and any(g in text.lower() for g in GOODBYE):
                self.metrics.end_reason = "caller_finished"
                await self._end()
                return

            self._reply = asyncio.create_task(self._respond(text, lang, turn))
            with suppress(asyncio.CancelledError):
                await self._reply
            if self._pending_end is not None and not self._ending:
                outcome = self._pending_end
                self.metrics.end_reason = outcome.end_reason or "agent_ended"
                # If the model never said goodbye after ending the call, the closing line does.
                await self._end(say_closing=not outcome.skip_closing or not self._goodbye_said)
                return

    async def _respond(self, text: str, lang: Lang, turn=None) -> None:
        started = time.monotonic()

        # The model starts immediately; sentences (and filler markers) are queued as they complete.
        sentences: asyncio.Queue = asyncio.Queue()

        async def produce() -> None:
            got_any = False
            try:
                async for sentence in self.answer_sentences(text, lang):
                    if not isinstance(sentence, tuple):
                        got_any = True
                    await sentences.put(sentence)
                if not got_any:
                    self.metrics.unanswered += 1
                    # The model ran out of lookups without a word (an empty knowledge base sends it
                    # searching again and again): say something rather than leave the caller in silence.
                    if self._pending_end is None and not self._ending:
                        await sentences.put((_PHRASE, "no_answer"))
            except AllProvidersFailed:
                log.error("call %s: no LLM provider available", self.info.call_id)
                if not got_any:
                    await sentences.put((_PHRASE, "transfer"))
            finally:
                await sentences.put(None)

        def is_phrase(item) -> bool:
            return isinstance(item, tuple) and item[0] is _PHRASE

        producer = asyncio.create_task(produce())
        try:
            wait = self.deps.filler_after_ms / 1000
            try:
                first = await (asyncio.wait_for(sentences.get(), wait) if wait > 0 else sentences.get())
            except asyncio.TimeoutError:
                await self._say_phrase("thinking", lang)
                first = await sentences.get()
            first_ready = time.monotonic()
            self.metrics.replies.append(first_ready - started)

            item = first
            first_audio = None
            while item is not None:
                if is_phrase(item):
                    await self._say_phrase(item[1], lang)
                    item = await sentences.get()
                    continue

                async def spoken():
                    nonlocal item
                    while item is not None and not is_phrase(item):
                        yield self._spoken(item, lang)
                        item = await sentences.get()

                result = await self.speech.speak_stream(spoken(), lang)
                first_audio = first_audio or result.first_audio_at
            if turn is not None and first_audio is not None:
                # loop.time() and time.monotonic() share a clock on the default event loop.
                log.info("call %s turn timing: waited for transcript %.0fms, answer %.0fms, "
                         "first audio %.0fms after caller stopped",
                         self.info.call_id,
                         (turn.committed_at - turn.speech_ended_at) * 1000,
                         (first_ready - started) * 1000,
                         (first_audio - turn.speech_ended_at) * 1000)
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
        self.silence.arm()

    # ---- lifecycle

    async def _start_conversation(self) -> None:
        try:
            await asyncio.wait_for(self.conversation.start(), self.deps.start_timeout_s)
        except asyncio.TimeoutError:
            log.warning("call %s: caller lookup exceeded %.1fs; continuing without it",
                        self.info.call_id, self.deps.start_timeout_s)
        except Exception:  # noqa: BLE001 - a lookup failure must not drop the call
            log.exception("call %s: caller lookup failed", self.info.call_id)
        if self.info.language_hint is None:
            self.lang.current = self.conversation.initial_language
            self.lang.used = {self.lang.current}

    async def run(self) -> CallMetrics:
        await self._start_conversation()
        text, key = self.conversation.opening(self.lang.current)
        await self._say(text, self.lang.current, phrase_key=key)
        self.silence.arm()
        self._tasks = [asyncio.create_task(self._event_loop()),
                       asyncio.create_task(self._turn_loop())]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the call handler itself is being cancelled
            # otherwise the loops were cancelled because the call ended
        except Exception:
            self.metrics.end_reason = "error"
            log.exception("call %s failed", self.info.call_id)
        finally:
            for task in self._tasks:
                task.cancel()
            self.turns.cancel()
            self.silence.disarm()
            if not self._ending:
                await self._end()
        return self.metrics

    def record(self) -> CallRecord:
        # Speech-to-text streams every second of the call; synthesised characters are counted by the
        # speech output (cached phrases excluded).
        self.cost.stt_seconds = self.metrics.duration_s
        self.cost.tts_chars = getattr(self.speech, "synth_chars", 0)
        return CallRecord(info=self.info, transcript=list(self.transcript),
                          languages_used=sorted(self.lang.used), final_language=self.lang.current,
                          duration_s=round(self.metrics.duration_s, 2),
                          end_reason=self.metrics.end_reason, metrics=self.metrics.summary(), cost=self.cost)

    async def _end(self, say_closing: bool = True) -> None:
        if self._ending:
            return
        self._ending = True
        if say_closing:
            with suppress(Exception):
                await self._say_phrase("closing", self.lang.current)
        result = self.hangup()
        if asyncio.iscoroutine(result):
            await result
        for task in self._tasks:
            task.cancel()

    async def on_caller_hangup(self) -> None:
        if not self._ending:
            self.metrics.end_reason = "caller_hangup"
        self._ending = True
        for task in self._tasks:
            task.cancel()
