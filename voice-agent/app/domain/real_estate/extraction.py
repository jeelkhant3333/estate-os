"""End-of-call structured extraction.

One model call over the transcript and the tool results, constrained to a JSON object and checked
with pydantic. What the tools established during the call (a booked visit, saved requirements,
prices quoted) always wins over what the model reads back out of the transcript; the model only
fills gaps (a summary, a name, a timeline it heard but nobody saved). If the model is unavailable
or returns something unusable, the record is built from the call state alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from app.llm.base import Message, TextDelta

log = logging.getLogger(__name__)


_EMPTY = ("", "null", "unknown", "NA")


_ALLOWED = {
    "intent": {"BUY", "RENT"},
    "property_type": {"APARTMENT", "VILLA", "PLOT", "COMMERCIAL"},
    "purpose": {"SELF_USE", "INVESTMENT"},
    "possession_preference": {"READY", "UNDER_CONSTRUCTION", "ANY"},
    "sentiment": {"POSITIVE", "NEUTRAL", "NEGATIVE"},
}
_ALIASES = {
    "intent": {"INVESTMENT": "BUY", "INVEST": "BUY", "PURCHASE": "BUY", "BUYING": "BUY", "SALE": "BUY",
               "LEASE": "RENT", "RENTAL": "RENT", "RENTING": "RENT"},
    "property_type": {"FLAT": "APARTMENT", "APARTMENTS": "APARTMENT", "HOUSE": "VILLA", "BUNGALOW": "VILLA",
                      "ROW_HOUSE": "VILLA", "LAND": "PLOT", "OFFICE": "COMMERCIAL", "SHOP": "COMMERCIAL",
                      "CORPORATE_OFFICE": "COMMERCIAL", "WAREHOUSE": "COMMERCIAL", "RETAIL": "COMMERCIAL"},
    "purpose": {"SELF": "SELF_USE", "END_USE": "SELF_USE", "OWN_USE": "SELF_USE", "INVEST": "INVESTMENT"},
}


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str | None = Field(default=None, max_length=2000)
    customer_name: str | None = Field(default=None, max_length=120)
    intent: Literal["BUY", "RENT"] | None = None
    budget_min_inr: int | None = Field(default=None, ge=0)
    budget_max_inr: int | None = Field(default=None, ge=0)
    bhk: list[int] = Field(default_factory=list, max_length=4)
    location: str | None = Field(default=None, max_length=120)
    property_type: Literal["APARTMENT", "VILLA", "PLOT", "COMMERCIAL"] | None = None
    timeline: str | None = Field(default=None, max_length=80)
    timeline_months: int | None = Field(default=None, ge=0, le=120)
    purpose: Literal["SELF_USE", "INVESTMENT"] | None = None
    possession_preference: Literal["READY", "UNDER_CONSTRUCTION", "ANY"] | None = None
    questions_asked: list[str] = Field(default_factory=list, max_length=30)
    unanswered_questions: list[str] = Field(default_factory=list, max_length=30)
    sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if value in _EMPTY else value

    @field_validator("intent", "property_type", "purpose", "possession_preference", "sentiment", mode="before")
    @classmethod
    def _known_value(cls, value: Any, info: ValidationInfo) -> Any:
        """Close answers are mapped ("INVESTMENT" is a purchase); anything else is unknown, never a
        reason to throw away the whole post-call extraction."""
        if value is None:
            return None
        v = re.sub(r"[^A-Z]+", "_", str(value).upper()).strip("_")
        aliases = _ALIASES.get(info.field_name, {})
        v = aliases.get(v, v)
        allowed = _ALLOWED[info.field_name]
        return v if v in allowed else None

    @field_validator("bhk", "questions_asked", "unanswered_questions", mode="before")
    @classmethod
    def _as_list(cls, value: Any, info: ValidationInfo) -> Any:
        # The model sometimes answers a one-item list as a bare value, or an empty one as null; one
        # malformed field must not throw away the whole post-call extraction.
        if value is None or value in _EMPTY:
            return []
        items = value if isinstance(value, list) else [value]
        if info.field_name == "bhk":
            # "2-3", "2 or 3", "3 BHK": the configurations as integers.
            return [int(n) for item in items for n in re.findall(r"\d+", str(item))]
        return items


SCHEMA_HINT = json.dumps({
    "summary": "2-4 sentences in English: what they want and the agreed next step",
    "customer_name": "string|null", "intent": "BUY|RENT|null", "budget_min_inr": "int|null",
    "budget_max_inr": "int|null", "bhk": "[int]", "location": "string|null",
    "property_type": "APARTMENT|VILLA|PLOT|COMMERCIAL|null", "timeline": "string|null",
    "timeline_months": "int|null", "purpose": "SELF_USE|INVESTMENT|null",
    "possession_preference": "READY|UNDER_CONSTRUCTION|ANY|null", "questions_asked": "[string]",
    "unanswered_questions": "[string]", "sentiment": "POSITIVE|NEUTRAL|NEGATIVE|null"})

PROMPT = ("You turn a real-estate phone call into a CRM record. Use ONLY what was said or returned by "
          "tools; use null when something is unknown. Amounts are whole rupees (1.2 crore = 12000000). "
          "Return one JSON object with exactly these keys and nothing else:\n" + SCHEMA_HINT)


def _first_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def extract(provider, transcript: list[dict[str, Any]], tool_log: list[dict[str, Any]],
                  timeout_s: float = 20.0) -> Extraction:
    lines = [f"{t.get('speaker')}: {t.get('text')}" for t in transcript][-120:]
    tools = [f"{t['tool']}({json.dumps(t.get('args'), ensure_ascii=False)}) -> "
             f"{json.dumps(t.get('result'), ensure_ascii=False, default=str)[:400]}" for t in tool_log][-30:]
    messages = [Message("system", PROMPT),
                Message("user", "TRANSCRIPT:\n" + "\n".join(lines) + "\n\nTOOL RESULTS:\n" + "\n".join(tools))]

    async def run() -> str:
        text = ""
        async for event in provider.stream(messages, [], max_tokens=700, temperature=0.0):
            if isinstance(event, TextDelta):
                text += event.text
        return text

    try:
        raw = await asyncio.wait_for(run(), timeout_s)
    except Exception as exc:  # noqa: BLE001 - extraction is best effort; the call state is enough
        log.warning("post-call extraction failed: %r", exc)
        return Extraction()
    data = _first_object(raw)
    if data is None:
        return Extraction()
    try:
        return Extraction.model_validate(data)
    except ValidationError as exc:
        log.warning("post-call extraction did not validate: %s", exc.errors()[:2])
        # Keep the fields that are individually valid.
        clean = {}
        for key, value in data.items():
            try:
                Extraction.model_validate({key: value})
                clean[key] = value
            except ValidationError:
                continue
        return Extraction.model_validate(clean)
