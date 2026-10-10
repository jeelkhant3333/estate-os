"""Checks applied in code, around the model rather than inside its prompt."""

from __future__ import annotations

import re
from typing import Any

from .money import _DEV_DIGITS, CRORE, LAKH, amounts_in, caller_amounts, words_to_digits

_DNC = re.compile(
    r"(do\s*n[o']?t\s+call|don'?t\s+call|stop\s+calling|never\s+call|remove\s+my\s+number|"
    r"call\s+(?:mat|matt)\s+(?:karo|kariye|karna|kijiye|karein)|phone\s+(?:mat|matt)\s+(?:karo|kariye|karna)|"
    r"(?:dobara|phir\s+se|firse)\s+(?:call|phone)\s+(?:mat|matt)|"
    r"कॉल\s+मत\s+(?:करो|करें|कीजिए|करना)|फ़?फोन\s+मत\s+(?:करो|करें|कीजिए|करना)|दोबारा\s+(?:कॉल|फ़?फोन)\s+मत|"
    r"(?:फोन|कॉल)\s+करू\s+नका|(?:phone|call)\s+karu\s+naka|पुन्हा\s+(?:फोन|कॉल)\s+करू\s+नका|"
    r"(?:ફોન|કૉલ|કોલ)\s+(?:ના|ન)\s+કરશો|(?:phone|call)\s+na\s+kar(?:so|sho|jo)|ફરી\s+(?:ફોન|કૉલ)\s+(?:ના|ન))",
    re.I)
_WRONG_NUMBER = re.compile(
    r"(wrong\s+number|galat\s+number|गलत\s+नंबर|चुकीचा\s+नंबर|chukicha\s+number|ખોટો\s+નંબર|khoto\s+number)", re.I)
_HUMAN = re.compile(
    r"(real\s+person|human|talk\s+to\s+(?:a\s+)?(?:person|someone|agent|manager|executive)|"
    r"(?:agent|manager|executive|insaan|aadmi)\s+se\s+baat|kisi\s+(?:insaan|aadmi|person)|"
    r"इंसान\s+से\s+बात|किसी\s+व्यक्ति|मैनेजर\s+से|माणसाशी\s+बोल|व्यक्तीशी\s+बोल|"
    r"માણસ\s+સાથે\s+વાત|મેનેજર\s+સાથે)", re.I)
_NEGOTIATION = re.compile(
    r"(discount|negotiat|best\s+price|final\s+price|kam\s+(?:karo|kariye|kar\s+do)|kuch\s+kam|"
    r"booking\s+amount|token\s+amount|डिस्काउंट|कम\s+कर|सवलत|कमी\s+कर|ડિસ્કાઉન્ટ|ઓછું\s+કર)", re.I)
_LEGAL = re.compile(
    r"(agreement|sale\s+deed|registry|registration\s+charges|stamp\s+duty|legal|title\s+(?:deed|clear)|"
    r"court|litigation|oc\b|occupancy\s+certificate|एग्रीमेंट|रजिस्ट्री|करारनामा|દસ્તાવેજ)", re.I)


def wants_dnc(text: str) -> bool:
    return bool(text and _DNC.search(text))


def wrong_number(text: str) -> bool:
    return bool(text and _WRONG_NUMBER.search(text))


def asks_for_human(text: str) -> bool:
    return bool(text and _HUMAN.search(text))


def escalation_topic(text: str) -> str | None:
    if _NEGOTIATION.search(text or ""):
        return "NEGOTIATION"
    if _LEGAL.search(text or ""):
        return "LEGAL"
    return None


# A sentence that states an area. Its figures must be ones a tool returned in this call.
_AREA_UNIT = re.compile(r"(sq\.?\s*f(?:ee)?t|sqft|square\s*f(?:ee|oo)t|carpet|built[\s-]*up|super\s*built|"
                        r"वर्ग\s*फ़?फु?ट|स्क्वेयर\s*फ़?फीट|स्क्वायर\s*फ़?फीट|चौरस\s*फूट|ચોરસ\s*ફૂટ|સ્ક્વેર\s*ફૂટ)", re.I)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[int]:
    """Every whole number written in the text (digits, with Indian or Western grouping)."""
    out: set[int] = set()
    for token in _NUMBER.findall((text or "").translate(_DEV_DIGITS)):
        try:
            out.add(int(float(token.replace(",", ""))))
        except ValueError:
            continue
    return out


class PriceGuard:
    """Only rupee amounts that came from this call's tool results (or the caller) may be spoken.

    The model writes amounts in digits ("85 lakh", "₹40 per sq ft"). Each amount in a sentence must
    match one seen in a tool result — exactly, or within 2% for amounts of a lakh or more, which
    covers saying ₹85,12,000 as "85 lakh". The caller's own figures (their budget) count too, so
    reading a budget back is allowed.
    """

    def __init__(self) -> None:
        self.evidence: set[int] = set()
        # Every number seen in tool results (and the caller's words): the evidence for areas.
        self.numbers: set[int] = set()

    def add_text(self, text: str) -> None:
        self.evidence.update(amounts_in(text))
        self.numbers.update(numbers_in(text))

    def add_caller_text(self, text: str) -> None:
        """The caller's own words: their figures may be read back, also when spoken as words
        ("साठ से अस्सी लाख") and both ends of a range."""
        self.evidence.update(caller_amounts(text))
        self.numbers.update(numbers_in(words_to_digits(text)))

    def add_result(self, value: Any, prices: bool = True) -> None:
        """Record a tool result as evidence. prices=False (documents): its numbers may support an
        area or count, never a rupee amount."""
        def walk(node: Any, key: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)
            elif isinstance(node, str):
                if prices:
                    self.add_text(node)
                else:
                    self.numbers.update(numbers_in(node))
            elif isinstance(node, (int, float)) and not isinstance(node, bool):
                self.numbers.add(int(round(node)))
                if not prices:
                    return
                lowered = key.lower()
                if any(word in lowered for word in ("inr", "price", "amount", "budget", "charge")) and node >= 1:
                    self.evidence.add(int(round(node)))

        walk(value)

    def supported(self, amount: int) -> bool:
        for seen in self.evidence:
            if amount == seen:
                return True
            if seen >= LAKH and abs(amount - seen) <= 0.02 * seen:
                return True
            if seen >= CRORE and abs(amount - seen) < LAKH:
                return True
        return False

    def unsupported(self, sentence: str) -> list[int]:
        # Riya writes numbers as words ("साढ़े छिहत्तर लाख", "सात सौ बीस"); read them as figures.
        sentence = words_to_digits(sentence)
        return [a for a in amounts_in(sentence) if not self.supported(a)] + self.unsupported_areas(sentence)

    def unsupported_areas(self, sentence: str) -> list[int]:
        """Figures of 100 or more in a sentence about area that no tool returned in this call.

        The model has stated plausible but invented carpet and built-up areas; an area is only spoken
        when it came from the CRM or a document returned by a tool.
        """
        if not _AREA_UNIT.search(sentence or ""):
            return []
        money = set(amounts_in(sentence))
        return sorted(n for n in numbers_in(sentence)
                      if n >= 100 and n not in self.numbers and n not in money)
