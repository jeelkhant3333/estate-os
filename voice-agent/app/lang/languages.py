"""Caller-language detection (spec section 16): follow the caller, never announce it."""

from __future__ import annotations

import re
from typing import Literal

from app.lang.text import devanagari_ratio, tokenize

# Riya speaks Hindi, Marathi and English.
Lang = Literal["mr", "hi", "en"]
LANGS: tuple[Lang, ...] = ("mr", "hi", "en")

_MR = {
    "आहे", "आहेत", "नाही", "मला", "तुम्ही", "आपण", "पाहिजे", "हवं", "हवा", "हवी", "काय", "कसं",
    "कधी", "किती", "बरोबर", "हो", "नको", "मी", "आम्ही", "तुमचं", "माझं", "साठी", "आणि", "पण",
    "येईल", "सांगा", "बघा", "करायचं", "घ्यायचं", "आता", "उद्या", "परवा", "कुठे", "इथे", "तिथे",
}
_HI = {
    "है", "हैं", "नहीं", "मुझे", "आप", "चाहिए", "क्या", "कैसे", "कब", "कितना", "कितने", "ठीक",
    "हाँ", "हां", "मैं", "हम", "के", "का", "की", "में", "से", "को", "रहा", "रही", "और", "लेकिन",
    "बताइए", "कल", "परसों", "कहाँ", "यहाँ", "वहाँ", "था", "थी",
}
_MR_ROMAN = {"aahe", "ahe", "mala", "pahije", "kiti", "kadhi", "barobar", "nako", "tumhi", "aapan", "hava", "havi", "udya", "parva", "kuthe"}
_HI_ROMAN = {"hai", "hain", "mujhe", "chahiye", "kitna", "kitne", "kab", "kya", "aap", "nahin", "theek", "haan", "kal", "parso", "kahan"}
# Scripts of languages Riya does not speak (Bengali, Gurmukhi, Gujarati, Odia, Tamil, Telugu,
# Kannada, Malayalam). The recogniser emits them for mumbles and line noise on Indian calls ("અ ಆ ಸತ್ಯ"); such
# a transcript is not evidence of what the caller speaks.
_FOREIGN_SCRIPT = ((0x0980, 0x09FF), (0x0A00, 0x0A7F), (0x0A80, 0x0AFF), (0x0B00, 0x0B7F), (0x0B80, 0x0BFF),
                   (0x0C00, 0x0C7F), (0x0C80, 0x0CFF), (0x0D00, 0x0D7F))


def has_foreign_script(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text or "" for lo, hi in _FOREIGN_SCRIPT)


def _script_of(ch: str) -> int | None:
    for lo, hi in _FOREIGN_SCRIPT:
        if lo <= ord(ch) <= hi:
            return lo
    return None


# Gurmukhi marks without a Devanagari letter at the same offset: addak (consonant doubling) is
# dropped, tippi is the anusvara.
_GURMUKHI_MARKS = {"\u0a71": "", "\u0a70": "\u0902"}


def to_devanagari_script(text: str) -> str:
    """Indic scripts share one letter layout (ISCII), so Punjabi, Bengali, Gujarati, Odia... letters map
    to Devanagari by a fixed offset. The recogniser sometimes writes Hindi speech in another script
    ("ਸੱਠ ਤੋਂ ਸੱਤਰ ਲੱਖ"); converted, it reads as Hindi instead of being lost."""
    out = []
    for ch in text or "":
        if ch in _GURMUKHI_MARKS:
            out.append(_GURMUKHI_MARKS[ch])
            continue
        base = _script_of(ch)
        out.append(chr(ord(ch) - base + 0x0900) if base is not None else ch)
    return "".join(out)


def is_noise(text: str) -> bool:
    """A transcript that is line noise rather than speech: nothing, a single sound ("O", "अ"), or a
    jumble of several different scripts in one breath ("અ ಆ ಸತ್ಯ"), which is what the recogniser emits
    for background noise. Speech written in one other Indic script is not noise: it is converted
    to Devanagari (to_devanagari_script), because it is usually Hindi spelt in the wrong script."""
    toks = tokenize(text or "")
    if not toks:
        return True
    scripts = {s for ch in text for s in [_script_of(ch)] if s is not None}
    if len(scripts) >= 2:
        return True
    return len(toks) == 1 and len(toks[0]) <= 1


_STT_HINT = {"mr-IN": "mr", "hi-IN": "hi", "en-IN": "en", "mr": "mr", "hi": "hi", "en": "en"}


def detect_language(text: str, stt_language: str | None = None) -> tuple[Lang | None, float]:
    """Return (language, confidence 0..1). None when there is not enough signal."""
    toks = tokenize(text)
    scores = {"mr": 0.0, "hi": 0.0, "en": 0.0}
    for tok in toks:
        if tok in _MR or tok in _MR_ROMAN:
            scores["mr"] += 1
        if tok in _HI or tok in _HI_ROMAN:
            scores["hi"] += 1
    foreign = has_foreign_script(text)
    if "ळ" in text:
        scores["mr"] += 1.5
    hint = _STT_HINT.get(stt_language or "")
    dev = devanagari_ratio(text)
    # The recogniser's tag counts only when the script agrees: "Tuesday छे बजे" tagged en-IN is
    # Hindi with an English word in it, not a switch to English.
    if hint and not foreign and not (hint == "en" and dev >= 0.2):
        scores[hint] += 1.5
    # Latin script is weak evidence for English. Sarvam's codemix mode transcribes Hindi and
    # Marathi speech in Latin too, and a one-word reply ("ok", "hello") carries no language at
    # all — treating either as confident English flips the whole call into the wrong language.
    # So English needs a few words of its own, and never overrides the recogniser's own tag.
    if (dev < 0.2 and not foreign and scores["mr"] == 0 and scores["hi"] == 0
            and len(toks) >= 3 and hint not in ("hi", "mr")):
        scores["en"] += 1 + min(len(toks), 4) * 0.25
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    if scores[best] == 0:
        return None, 0.0
    return best, scores[best] / total  # type: ignore[return-value]


_ASK_LANGUAGE = {
    "hi": re.compile(r"(हिंदी|हिन्दी|hindi)\s*(में|me|mein|मे)", re.I),
    "mr": re.compile(r"(मराठी|marathi)\s*(मध्ये|में|me|mein|मधे|त)", re.I),
    "en": re.compile(r"(english|इंग्लिश|अंग्रेज़ी|अंग्रेजी)\s*(में|me|mein|मध्ये|please)|\bin\s+english\b|speak\s+english", re.I),
}


def asked_for_language(text: str) -> Lang | None:
    """The caller asks to be spoken to in a language: "हिंदी में बात करिए", "speak English"."""
    for lang, pattern in _ASK_LANGUAGE.items():
        if pattern.search(text or ""):
            return lang  # type: ignore[return-value]
    return None


class LanguageTracker:
    """Keeps the call language stable; switches only on a confident change.

    Hindi and Marathi share much of their common vocabulary, so a genuine Hindi utterance often
    scores for both and lands near the middle of the confidence range. The threshold is therefore
    configurable, and the STT's own language tag can settle it outright: when the recogniser and
    the wording agree, there is nothing to be gained by waiting for another turn.
    """

    def __init__(self, initial: Lang, switch_confidence: float = 0.6):
        self.current: Lang = initial
        # Set when the caller asks for a language ("हिंदी में बात करिए"): kept for the rest of the call.
        self.locked = False
        self.switches = 0
        self.used: set[Lang] = {initial}
        self.switch_confidence = switch_confidence

    def observe(self, text: str, stt_language: str | None = None) -> Lang:
        asked = asked_for_language(text)
        if asked:
            if asked != self.current:
                self.switches += 1
            self.current, self.locked = asked, True
            self.used.add(asked)
            return self.current
        if self.locked:
            return self.current
        lang, confidence = detect_language(text, stt_language)
        hinted = _STT_HINT.get(stt_language or "")
        # The recogniser identified the language and the wording agrees: switch on this turn.
        decisive = hinted is not None and lang == hinted
        # A one-word reply ("जी", "ok") or a garbled transcript in another script never switches the
        # call: it once flipped a Hindi call into another language for several turns.
        toks = tokenize(text)
        substantial = len(toks) >= 2 and not has_foreign_script(text)
        # English needs a real sentence: "Near by" or "Cool" inside a Hindi call is not a switch.
        if lang == "en" and len(toks) < 3:
            substantial = False
        # Hindi and Marathi share a script: a switch between them needs the other language's own words
        # ("आहे", "मला"), not a ळ in a place name ("वाकळ") or the recogniser's tag alone, which once
        # flipped a Hindi caller into Marathi for most of a call.
        if lang in ("hi", "mr") and self.current in ("hi", "mr") and lang != self.current:
            own = _MR if lang == "mr" else _HI
            other = _HI if lang == "mr" else _MR
            toks_set = set(toks)
            if len(toks_set & own) < 1 or len(toks_set & other) >= len(toks_set & own):
                substantial = False
        if lang and lang != self.current and substantial and (decisive or confidence >= self.switch_confidence):
            self.current = lang
            self.switches += 1
        if lang:
            self.used.add(lang)
        return self.current
