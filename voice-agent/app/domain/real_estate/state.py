"""Everything the domain knows about one call while it is happening."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .guardrails import PriceGuard

CALL_TYPES = ("INBOUND", "OUTBOUND_NEW_LEAD", "VISIT_REMINDER", "CALLBACK", "RE_ENGAGEMENT")


@dataclass
class Requirements:
    """What the caller wants. Filled from save_requirements and tool arguments, never invented."""

    name: str | None = None
    intent: str | None = None                 # BUY | RENT
    budget_min: int | None = None
    budget_max: int | None = None
    bhk: list[int] = field(default_factory=list)
    locality: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    property_type: str | None = None          # APARTMENT | VILLA | PLOT | COMMERCIAL
    possession: str | None = None             # READY | UNDER_CONSTRUCTION | ANY
    timeline_months: int | None = None
    timeline_text: str | None = None
    purpose: str | None = None                # SELF_USE | INVESTMENT

    def known(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}

    def missing(self) -> list[str]:
        gaps = []
        if not self.intent:
            gaps.append("buy or rent")
        if self.budget_max is None and self.budget_min is None:
            gaps.append("budget")
        if not self.bhk:
            gaps.append("BHK")
        if not self.locality and not self.project_id:
            gaps.append("preferred locality or project")
        if not self.property_type:
            gaps.append("property type")
        if not self.possession and self.timeline_months is None:
            gaps.append("possession timeline")
        if not self.purpose:
            gaps.append("self-use or investment")
        return gaps


@dataclass
class CallState:
    call_type: str = "INBOUND"
    call_id: str = ""
    request_id: str | None = None            # outbound request id = CRM voice session externalId
    phone: str | None = None
    lead: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    appointment_id: str | None = None
    callback_id: str | None = None
    language: str = "hi"
    stage: str = "OPENING"
    started_at: float = field(default_factory=time.monotonic)
    caller_turns: int = 0
    requirements: Requirements = field(default_factory=Requirements)
    guard: PriceGuard = field(default_factory=PriceGuard)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    recommended: list[dict[str, Any]] = field(default_factory=list)
    budget_fits: bool = False
    offered_slots: dict[str, str] = field(default_factory=dict)   # start -> spoken label
    booked_visit: dict[str, Any] | None = None
    visit_declined: bool = False
    visit_outcome: str | None = None
    callback_at: str | None = None
    handover_reason: str | None = None
    asked_to_book: bool = False
    # The caller's latest turn asked about a visit (reset every turn).
    wants_visit_now: bool = False
    # Riya's last reply asked whether they would like to visit.
    visit_question_asked: bool = False
    # The caller wants details first / not now: no visit offers until they bring it up.
    visit_deferred: bool = False
    # Projects whose visit times were already offered in this call.
    slots_offered_for: list[str] = field(default_factory=list)
    # What the caller asked for that the builder does not sell ("office", "villa"), said once by Riya.
    not_sold: str | None = None
    # How many times Riya has asked about a visit (a visit is optional: once, unless the caller asks).
    visit_offers: int = 0
    # The project the caller is talking about; visits are offered and booked only for it.
    focus_project_id: str | None = None
    focus_project_name: str | None = None
    project_confirmed: str | None = None  # a visit project the caller was asked to confirm
    last_caller_text: str = ""
    # Sentences already spoken (normalised), so none is said twice unless the caller asks.
    spoken: list[str] = field(default_factory=list)
    caller_asked_repeat: bool = False
    # A budget heard far outside our prices ("780 लाख"): confirmed once with the caller before searching.
    budget_to_confirm: int | None = None
    budgets_confirmed: list[int] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    readbacks: dict[str, int] = field(default_factory=lambda: {"confirmed": 0, "corrected": 0})
    whatsapp_consent: bool | None = None
    whatsapp_requests: list[str] = field(default_factory=list)
    whatsapp_offered: bool = False
    dnc: bool = False
    dnc_basis: str | None = None
    wrong_number: bool = False
    identity_confirmed: bool = False
    ended_by_agent: str | None = None
    agent_actions: list[str] = field(default_factory=list)

    @property
    def lead_id(self) -> str | None:
        value = self.lead.get("id")
        return str(value) if value else None

    @property
    def lead_name(self) -> str | None:
        name = self.requirements.name or self.lead.get("customerName") or self.context.get("leadName") or self.lead.get("name")
        if name and str(name).startswith("Caller "):
            return None  # find-or-create placeholder, not a real name
        return name

    def action(self, text: str) -> None:
        if text not in self.agent_actions and len(self.agent_actions) < 50:
            self.agent_actions.append(text)
