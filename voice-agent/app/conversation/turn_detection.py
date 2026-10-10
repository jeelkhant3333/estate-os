"""Dynamic end-of-turn detection (spec sections 26-27).

Silence required after speech depends on whether the utterance so far looks complete, and grows
for callers who pause mid-sentence. The semantic model is pluggable; the default is a lexical
heuristic until the turn-detection benchmark (C6) picks a model.
"""

from __future__ import annotations

import re

import asyncio
from dataclasses import dataclass
from typing import Callable, Protocol

from app.lang.text import tokenize

CONTINUATION_WORDS = frozenset({
    # Marathi
    "आणि", "पण", "म्हणजे", "की", "तर", "मला", "माझं", "माझी", "माझा", "आमचं", "जे", "जो", "जी", "कारण",
    "किंवा", "साठी", "मध्ये", "अं", "अम्म", "म्हणून", "जर",
    # Hindi
    "और", "लेकिन", "मतलब", "कि", "तो", "मुझे", "मेरा", "मेरी", "हमारा", "क्योंकि", "या", "के", "का", "की",
    "में", "से", "को", "वाला", "वाली", "अगर", "जैसे",
    # English
    "and", "but", "so", "because", "or", "the", "a", "an", "to", "in", "with", "for", "of", "my", "i",
    "like", "um", "uh", "which", "that", "if",
    # Romanised
    "aur", "lekin", "matlab", "ki", "mujhe", "mera", "kyunki", "ya", "mein", "se", "ko", "ani", "pan",
    "mhanje", "mala", "majha", "karan", "kinva", "sathi", "madhye",
})


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def looks_incomplete(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return True
    if stripped.endswith(("...", "…", ",")):
        return True
    toks = tokenize(stripped)
    return bool(toks) and toks[-1] in CONTINUATION_WORDS


class EndOfTurnModel(Protocol):
    def incomplete(self, text: str) -> bool: ...


class LexicalEndOfTurn:
    def incomplete(self, text: str) -> bool:
        return looks_incomplete(text)


@dataclass
class TurnConfig:
    # Commit still waits for the STT final, so a short base silence does not cut callers off (CL-007).
    base_silence_ms: int = 120
    incomplete_silence_ms: int = 650
    max_silence_ms: int = 1400
    per_pause_extra_ms: int = 200
    # After the silence, wait this long for the STT to finalise what it heard; commit on partial text only on timeout.
    final_wait_ms: int = 900
    # Gaps shorter than this are breaths between words, not pauses that extend the silence requirement.
    min_pause_ms: int = 300
    # Start the response this long into the silence if finalised text looks complete (0 disables). Disabled by
    # default: Saaras finals arrive ~600 ms after speech end, so there is no window to gain (CL-006).
    speculative_ms: int = 0


@dataclass
class Turn:
    text: str
    language: str | None
    speech_started_at: float
    speech_ended_at: float
    committed_at: float
    final_at: float | None = None


class TurnDetector:
    def __init__(self, config: TurnConfig | None = None, model: EndOfTurnModel | None = None,
                 on_tentative: Callable[[str, float, float], None] | None = None):
        self.config = config or TurnConfig()
        self.model = model or LexicalEndOfTurn()
        self.on_tentative = on_tentative
        self.turns: asyncio.Queue[Turn] = asyncio.Queue()
        self._timer: asyncio.Task | None = None
        self._clear()

    def _clear(self) -> None:
        self._finals: list[str] = []
        if not hasattr(self, "_last_commit"):
            self._last_commit: tuple[str, float] | None = None
        self._partial = ""
        self._language: str | None = None
        self._utterance_start: float | None = None
        self._speech_end: float | None = None
        self._pauses = 0
        self._final_at: float | None = None
        self.speaking = False

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    # ---- inputs
    def on_speech_start(self, t: float | None = None) -> None:
        t = self._now() if t is None else t
        if self._timer and not self._timer.done():
            self._timer.cancel()
            if self._speech_end is None or (t - self._speech_end) * 1000 >= self.config.min_pause_ms:
                self._pauses += 1
        if self._utterance_start is None:
            self._utterance_start = t
        self.speaking = True

    def on_partial(self, text: str) -> None:
        self._partial = text

    def on_final(self, text: str, language: str | None = None) -> None:
        if self._is_late_repeat(text):
            # The turn was committed on partial text before this final arrived; it is the same words,
            # not a new utterance (it once made Riya answer every Deepgram sentence twice).
            return
        if text.strip():
            self._finals.append(text.strip())
            try:
                self._final_at = self._now()
            except RuntimeError:  # no running loop (unit tests)
                self._final_at = None
        self._partial = ""
        if language:
            self._language = language

    def on_speech_end(self, t: float | None = None) -> None:
        t = self._now() if t is None else t
        self.speaking = False
        self._speech_end = t
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = asyncio.create_task(self._commit_when_silent())

    def restore(self, text: str, speech_started_at: float) -> None:
        """Put back the text of a turn committed too early, so the next commit contains all of it (CL-011)."""
        if text.strip():
            self._finals.insert(0, text.strip())
        if self._utterance_start is None or speech_started_at < self._utterance_start:
            self._utterance_start = speech_started_at

    def cancel(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._clear()

    # ---- internals
    def current_text(self) -> str:
        parts = self._finals + ([self._partial] if self._partial else [])
        return " ".join(parts).strip()

    def required_silence_ms(self) -> int:
        cfg = self.config
        ms = cfg.incomplete_silence_ms if self.model.incomplete(self.current_text()) else cfg.base_silence_ms
        if self._pauses:
            ms = min(cfg.max_silence_ms, ms + cfg.per_pause_extra_ms * self._pauses)
        return ms

    async def _commit_when_silent(self) -> None:
        start = self._speech_end if self._speech_end is not None else self._now()
        tentative_sent = False
        while True:
            elapsed_ms = (self._now() - start) * 1000
            if (self.on_tentative and not tentative_sent and self.config.speculative_ms
                    and elapsed_ms >= self.config.speculative_ms):
                text = " ".join(self._finals).strip()
                if text and not self._partial and not self.model.incomplete(text):
                    tentative_sent = True
                    self.on_tentative(text, self._utterance_start if self._utterance_start is not None else start, start)
            remaining = self.required_silence_ms() - elapsed_ms
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining / 1000, 0.02))
        waited = 0.0
        # Partials from streaming STT can be unrelated to the final text, so wait for the final to settle.
        while (not self._finals or self._partial) and waited < self.config.final_wait_ms:
            await asyncio.sleep(0.01)
            waited += 10
        text = self.current_text()
        if not text:
            self._clear()
            return
        turn = Turn(
            text=text,
            language=self._language,
            speech_started_at=self._utterance_start if self._utterance_start is not None else start,
            speech_ended_at=start,
            committed_at=self._now(),
            final_at=self._final_at,
        )
        self._clear()
        self._last_commit = (_norm(text), self._now())
        await self.turns.put(turn)

    def _is_late_repeat(self, text: str) -> bool:
        if self._last_commit is None or self._utterance_start is not None or self.speaking:
            return False  # the caller has started a new utterance since the commit
        committed, at = self._last_commit
        try:
            fresh = self._now() - at < 4.0
        except RuntimeError:
            return False
        final = _norm(text)
        return fresh and bool(final) and (final in committed or committed in final)
