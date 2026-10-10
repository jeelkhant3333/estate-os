"""Barge-in and backchannel handling (spec sections 23-25).

Customer speech stops Riya unless it is a short backchannel. During a read-back, every answer
counts (the session bypasses this detector in READBACK_LISTENING).
"""

from __future__ import annotations

import re

from enum import StrEnum

from app.lang.languages import is_noise
from app.lang.text import tokenize

BACKCHANNELS = frozenset({
    "hmm", "hm", "hmmm", "हं", "हम्म", "हम", "हूं", "हूँ", "haan", "han", "हां", "हाँ", "हो", "ho",
    "ok", "okay", "ओके", "acha", "achha", "अच्छा", "right", "बरं", "bara", "yes", "उह", "जी", "ji",
})


# Thinking sounds on their own: the caller is still deciding, not answering.
_HESITATION = re.compile(r"^(?:\W*(?:ह+म+|हम्+|हम्म+|उ+म्*|उम्+|अ+ह*|अं+|आ+|ए+ं*|एं+|hm+|h+m+|u+m+|uh+|ah+|er+m*|mm+)\W*)+$", re.I)


def is_hesitation(text: str) -> bool:
    """'हम', 'उम्', 'हम्म… अ', 'hmm': a pause filler, not an answer to reply to."""
    return bool(text and text.strip() and _HESITATION.match(text.strip()))


def is_backchannel(text: str) -> bool:
    toks = tokenize(text)
    return 0 < len(toks) <= 2 and all(t in BACKCHANNELS for t in toks)


class BargeIn(StrEnum):
    NONE = "none"
    PENDING = "pending"
    INTERRUPT = "interrupt"


class BargeInDetector:
    def __init__(self, min_speech_ms: int = 300, backchannel_max_ms: int = 700):
        self.min_speech_ms = min_speech_ms
        self.backchannel_max_ms = backchannel_max_ms
        self.reset()

    def reset(self) -> None:
        self.onset: float | None = None
        self.ended_at: float | None = None
        self.text = ""
        self.speaking = False

    def on_onset(self, t: float) -> None:
        self.onset, self.ended_at, self.text, self.speaking = t, None, "", True

    def on_text(self, text: str) -> None:
        self.text = text

    def on_speech_end(self, t: float) -> None:
        self.speaking = False
        self.ended_at = t

    def evaluate(self, now: float) -> BargeIn:
        if self.onset is None:
            return BargeIn.NONE
        text = self.text.strip()
        if text and is_noise(text):
            text = ""  # background noise transcribed as a stray sound: judge it on duration alone
        if text and not is_backchannel(text):
            return BargeIn.INTERRUPT
        if not self.speaking:
            return BargeIn.NONE
        duration_ms = (now - self.onset) * 1000
        limit = self.backchannel_max_ms if text else self.min_speech_ms
        return BargeIn.INTERRUPT if duration_ms >= limit else BargeIn.PENDING
