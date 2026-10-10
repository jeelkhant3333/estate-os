"""The tools Riya may call during a call.

Every tool has a strict JSON schema (no extra properties), its arguments are validated again here
with pydantic, it has a timeout, and — where it takes a moment — a filler phrase the engine plays
while it runs. Results are kept short and speakable, and everything a tool returns is recorded as
evidence for the price guard: a rupee figure the model says must have come from here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.crm.client import CrmError
from app.domain.base import Tool, ToolOutcome
from app.llm.base import ToolSpec

from .timeutil import as_utc_iso, clamp_to_calling_hours, now_ist, parse_when, spoken_time

if TYPE_CHECKING:
    from .conversation import RealEstateConversation

log = logging.getLogger(__name__)

DOC_TYPES = ["BROCHURE", "PRICE_SHEET", "PAYMENT_PLAN", "FAQ", "RERA", "LEGAL", "FLOOR_PLAN", "OTHER"]
PROPERTY_TYPES = ["APARTMENT", "VILLA", "PLOT", "COMMERCIAL"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeArgs(_Args):
    question: str = Field(min_length=2, max_length=300)
    project: str | None = Field(default=None, max_length=80)
    doc_types: list[Literal["BROCHURE", "PRICE_SHEET", "PAYMENT_PLAN", "FAQ", "RERA", "LEGAL", "FLOOR_PLAN",
                            "OTHER"]] | None = None


class SlotArgs(_Args):
    project: str = Field(min_length=1, max_length=80)
    preferred_day: str | None = Field(default=None, max_length=40)


class BookArgs(_Args):
    project: str = Field(min_length=1, max_length=80)
    slot_start: str = Field(min_length=10, max_length=40)
    unit: str | None = Field(default=None, max_length=40)


class RescheduleArgs(_Args):
    slot_start: str = Field(min_length=10, max_length=40)
    reason: str | None = Field(default=None, max_length=200)


class ReasonArgs(_Args):
    reason: str | None = Field(default=None, max_length=200)


class NoArgs(_Args):
    pass


class CallbackArgs(_Args):
    when: str = Field(min_length=1, max_length=60)
    reason: str | None = Field(default=None, max_length=200)


class HumanArgs(_Args):
    reason: Literal["CUSTOMER_ASKED", "NEGOTIATION", "LEGAL", "UNANSWERED"]
    question: str | None = Field(default=None, max_length=300)


class WhatsAppArgs(_Args):
    kind: Literal["BROCHURE", "VISIT_CONFIRMATION"]
    customer_agreed: bool


class EndArgs(_Args):
    reason: Literal["COMPLETED", "CALLBACK_SCHEDULED", "NOT_INTERESTED", "WRONG_NUMBER", "DO_NOT_CALL",
                    "CUSTOMER_BUSY"]


class SaveArgs(_Args):
    name: str | None = Field(default=None, max_length=80)
    intent: Literal["BUY", "RENT"] | None = None
    budget_min_inr: int | None = Field(default=None, ge=0, le=10_000_000_000)
    budget_max_inr: int | None = Field(default=None, ge=0, le=10_000_000_000)
    bhk: list[int] | None = Field(default=None, max_length=4)
    locality: str | None = Field(default=None, max_length=80)
    project: str | None = Field(default=None, max_length=80)
    property_type: Literal["APARTMENT", "VILLA", "PLOT", "COMMERCIAL"] | None = None
    possession: Literal["READY", "UNDER_CONSTRUCTION", "ANY"] | None = None
    timeline_months: int | None = Field(default=None, ge=0, le=120)
    purpose: Literal["SELF_USE", "INVESTMENT"] | None = None
    readback: Literal["confirmed", "corrected"] | None = None


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_PROJECT = {"type": "string", "description": "Project name as the caller said it, or its id."}
_SLOT = {"type": "string", "description": "A slot_start exactly as get_visit_slots returned it."}


class ToolBox:
    def __init__(self, conversation: "RealEstateConversation"):
        self.c = conversation
        self.s = conversation.state

    # ---------------------------------------------------------------- plumbing

    def _tool(self, name: str, description: str, schema: dict[str, Any], model: type[_Args], handler,
              timeout_s: float = 3.0, filler: str | None = "filler_search") -> Tool:
        async def run(arguments: dict[str, Any]) -> ToolOutcome:
            try:
                args = model.model_validate(arguments)
            except ValidationError as exc:
                return ToolOutcome({"error": "invalid_arguments",
                                    "details": [e["msg"] for e in exc.errors()][:3]})
            try:
                outcome = await handler(args)
            except CrmError as exc:
                log.warning("tool %s: CRM %s %s", name, exc.status, exc.code)
                outcome = ToolOutcome({"error": "crm_unavailable" if exc.status >= 500 else "crm_rejected",
                                       "code": exc.code,
                                       "instruction": "Apologise briefly and say our team will confirm."})
            self.s.tool_log.append({"tool": name, "args": arguments,
                                    "result": {k: v for k, v in outcome.content.items() if k != "chunks"}})
            # The uploaded documents are the only source of facts, prices included.
            self.s.guard.add_result(outcome.content)
            self.c.advance()
            return outcome

        return Tool(ToolSpec(name, description, schema), run, timeout_s=timeout_s, filler=filler)

    def _project(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return self.c.plugin.resolve_project(name)

    def _unknown_project(self, name: str) -> ToolOutcome:
        """A project with no visit calendar. Never offer another project in its place: the caller asked for
        this one, and booking a different one is worse than booking none."""
        request = f"Wants a site visit to {name}"[:300]
        if request not in self.s.unanswered:
            self.s.unanswered.append(request)
        if "UNANSWERED" not in self.s.escalations:
            self.s.escalations.append("UNANSWERED")
        self.s.handover_reason = self.s.handover_reason or "UNANSWERED"
        return ToolOutcome({"error": "no_visit_calendar", "project": name,
                            "instruction": f"Visits for {name} cannot be booked on this call. Do not offer or book "
                                           "any other project instead. Tell the caller our property expert will "
                                           "call them to fix the visit, and ask for a convenient time."})

    async def _lead_id(self) -> str | None:
        if self.s.lead_id:
            return self.s.lead_id
        if not self.s.phone:
            return None
        lead = await self.c.plugin.crm.find_or_create_lead(self.s.phone, self.s.language)
        self.s.lead = lead
        return self.s.lead_id

    # ---------------------------------------------------------------- inventory

    async def ask_knowledge(self, a: KnowledgeArgs) -> ToolOutcome:
        project = self._project(a.project) if a.project else None
        project_id = project["id"] if project else self.s.requirements.project_id
        result = await self.c.plugin.knowledge.retrieve(a.question, project_id, a.doc_types, self.s.language, k=3)
        self.c.plugin.last_rag_status = result.status
        if not result.chunks:
            if a.question not in self.s.unanswered:
                self.s.unanswered.append(a.question)
            if "UNANSWERED" not in self.s.escalations:
                self.s.escalations.append("UNANSWERED")
            return ToolOutcome({"found": False, "lookup": result.status,
                                "instruction": "Say a property expert will confirm this; do not guess. Do not look"
                                               " this question up again: the documents do not have it."})
        for chunk in result.chunks:
            doc = str(chunk.get("documentId"))
            if doc and doc not in self.s.citations:
                self.s.citations.append(doc)
        return ToolOutcome({"found": True, "chunks": [
            {"documentId": ch.get("documentId"), "title": ch.get("title"), "docType": ch.get("docType"),
             "page": ch.get("page"), "section": ch.get("sectionPath"), "text": ch.get("content")}
            for ch in result.chunks]})

    # ---------------------------------------------------------------- visits

    def _off_focus(self, project: dict[str, Any]) -> ToolOutcome | None:
        """A visit for a project other than the one the caller has been talking about is confirmed with the
        caller first. The model is never steered to the earlier project: that booked the wrong one."""
        focus = self.s.focus_project_id
        if not focus or str(project["id"]) == focus:
            return None
        named = self.c.mentioned_project(self.s.last_caller_text)
        if named is not None and str(named["id"]) == str(project["id"]):
            self.s.focus_project_id, self.s.focus_project_name = str(project["id"]), project["name"]
            return None
        if self.s.project_confirmed == str(project["id"]):
            self.s.focus_project_id, self.s.focus_project_name = str(project["id"]), project["name"]
            return None
        self.s.project_confirmed = str(project["id"])
        current = next((p["name"] for p in (self.c.plugin.catalog or {}).get("projects", []) if str(p["id"]) == focus),
                       "the earlier project")
        return ToolOutcome({"error": "confirm_project", "asked": project["name"], "earlier": current,
                            "instruction": f"Ask the caller in one short question whether the visit is for "
                                           f"{project['name']} or {current}, then use the one they say. "
                                           "Do not choose for them."})

    async def get_visit_slots(self, a: SlotArgs) -> ToolOutcome:
        project = self._project(a.project)
        if project is None:
            return self._unknown_project(a.project)
        if (off := self._off_focus(project)) is not None:
            return off
        about_existing_visit = self.s.call_type == "VISIT_REMINDER" or bool(self.s.context.get("visit"))
        if not a.preferred_day and not self.s.wants_visit_now and not about_existing_visit:
            if not self.s.slots_offered_for:
                # "Show me the Baner one" asks about the project, not for a visit. No "slots" key: an empty
                # list was once read out as "no slots are available".
                return ToolOutcome({"project": project["name"], "visitTimesLookedUp": False,
                                    "instruction": "Visit times exist but were not looked up, because the caller has "
                                                   "not asked for a visit. Never say no slots are available. Answer "
                                                   "what they asked, then ask once whether they would like to visit; "
                                                   "call get_visit_slots after they say yes."})
            if self.s.visit_deferred:
                return ToolOutcome({"project": project["name"], "slots": [],
                                    "instruction": "The caller wants details first. Do not offer visit times now; "
                                                   "answer their question."})
            if project["id"] in self.s.slots_offered_for:
                # The slot_start values must come back too: older turns' tool results are not kept in
                # the model's history, and a booking needs one of them.
                return ToolOutcome({"project": project["name"], "alreadyOffered": [
                    {"slot_start": start, "label": label} for start, label in self.s.offered_slots.items()],
                    "instruction": "You already offered these times. Do not read them again; answer the "
                                   "caller's question. To book, use one of these slot_start values."})
        wanted = parse_when(a.preferred_day) if a.preferred_day else None
        today = now_ist().date()
        start_day = wanted.date() if wanted and wanted.date() >= today else today
        result = await self.c.plugin.crm.get_slots(project["id"], start_day.isoformat(), 2 if wanted else 3)
        slots = result.get("slots", [])
        if wanted:
            same_day = [s for s in slots if s.get("date") == wanted.date().isoformat()]
            slots = same_day or slots
        if len(slots) > 3:  # spread the offer across the day(s) rather than three back-to-back hours
            slots = [slots[0], slots[len(slots) // 2], slots[-1]]
        for slot in slots:
            self.s.offered_slots[slot["start"]] = slot["label"]
        if slots and project["id"] not in self.s.slots_offered_for:
            self.s.slots_offered_for.append(project["id"])
        self.s.requirements.project_id = self.s.requirements.project_id or project["id"]
        if not slots:
            return ToolOutcome({"project": project["name"], "slots": [],
                                "instruction": "No free slots soon; offer a callback to fix a time."})
        return ToolOutcome({"project": project["name"], "timezone": "Asia/Kolkata",
                            "slots": [{"slot_start": s["start"], "label": s["label"]} for s in slots]})

    @staticmethod
    def _same_instant(a: str, b: str) -> bool:
        try:
            return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))
        except ValueError:
            return False

    def _offered(self, slot_start: str) -> str | None:
        """The offered slot the model means: its slot_start, or the label it was offered with."""
        for start in self.s.offered_slots:
            if self._same_instant(start, slot_start):
                return start
        wanted = " ".join(str(slot_start or "").lower().split())
        for start, label in self.s.offered_slots.items():
            if wanted and wanted == " ".join(str(label).lower().split()):
                return start
        return None

    async def book_site_visit(self, a: BookArgs) -> ToolOutcome:
        project = self._project(a.project)
        if project is None:
            return self._unknown_project(a.project)
        if (off := self._off_focus(project)) is not None:
            return off
        offered = self._offered(a.slot_start)
        if offered is None:
            if self.s.offered_slots:
                return ToolOutcome({"error": "slot_not_offered", "offered": [
                    {"slot_start": start, "label": label} for start, label in self.s.offered_slots.items()],
                    "instruction": "Book again with one of these slot_start values; do not ask for slots again."})
            return ToolOutcome({"error": "slot_not_offered",
                                "instruction": "Call get_visit_slots and book one of the offered slots."})
        lead_id = await self._lead_id()
        if lead_id is None:
            return ToolOutcome({"error": "no_lead", "instruction": "Say our team will call to confirm the visit."})
        try:
            visit = await self.c.plugin.crm.book_visit(lead_id, project["id"], offered, a.unit, None,
                                                       self.s.language, self.s.call_id)
        except CrmError as exc:
            if exc.status == 409:
                self.s.offered_slots.pop(offered, None)
                return ToolOutcome({"error": "slot_taken", "code": exc.code,
                                    "instruction": "That slot just filled; offer another of the slots."})
            raise
        self.s.booked_visit = {"id": str(visit.get("id")), "projectName": project["name"],
                               "spokenTime": visit.get("spokenTime") or self.s.offered_slots.get(offered),
                               "agentName": visit.get("agentName"), "status": visit.get("status")}
        self.s.appointment_id = self.s.appointment_id or str(visit.get("id"))
        self.s.action("book_site_visit")
        return ToolOutcome({"booked": True, "project": project["name"], "when": self.s.booked_visit["spokenTime"],
                            "agentName": visit.get("agentName"), "status": visit.get("status"),
                            "instruction": "Confirm aloud: project, day, time and the agent's name."})

    def _appointment(self) -> str | None:
        return self.s.appointment_id or (self.s.booked_visit or {}).get("id") or \
            str((self.s.context.get("visit") or {}).get("appointmentId") or "") or None

    async def reschedule_visit(self, a: RescheduleArgs) -> ToolOutcome:
        appointment = self._appointment()
        if not appointment:
            return ToolOutcome({"error": "no_visit", "instruction": "There is no visit to reschedule; offer to book one."})
        offered = self._offered(a.slot_start)
        if offered is None:
            return ToolOutcome({"error": "slot_not_offered", "instruction": "Call get_visit_slots first."})
        try:
            visit = await self.c.plugin.crm.reschedule_visit(appointment, offered, a.reason)
        except CrmError as exc:
            if exc.status == 409:
                return ToolOutcome({"error": "slot_taken", "instruction": "Offer another slot."})
            raise
        self.s.visit_outcome = "RESCHEDULED"
        self.s.action("reschedule_visit")
        return ToolOutcome({"rescheduled": True, "when": visit.get("spokenTime"), "agentName": visit.get("agentName")})

    async def cancel_visit(self, a: ReasonArgs) -> ToolOutcome:
        appointment = self._appointment()
        if not appointment:
            return ToolOutcome({"error": "no_visit"})
        await self.c.plugin.crm.cancel_visit(appointment, a.reason)
        self.s.visit_outcome = "CANCELLED"
        self.s.visit_declined = True
        self.s.action("cancel_visit")
        return ToolOutcome({"cancelled": True, "instruction": "Ask if a later date would suit; offer a callback."})

    async def confirm_visit(self, a: NoArgs) -> ToolOutcome:
        appointment = self._appointment()
        if not appointment:
            return ToolOutcome({"error": "no_visit"})
        visit = await self.c.plugin.crm.confirm_visit(appointment)
        self.s.visit_outcome = "CONFIRMED"
        self.s.action("confirm_visit")
        return ToolOutcome({"confirmed": True, "when": visit.get("spokenTime"), "agentName": visit.get("agentName")})

    # ---------------------------------------------------------------- follow-up

    async def _callback(self, due: datetime, reason: str | None, requested_by: str = "CUSTOMER") -> datetime:
        settings = self.c.plugin.settings
        due = clamp_to_calling_hours(due, settings.calling_hours_start, settings.calling_hours_end)
        lead_id = await self._lead_id()
        if lead_id:
            created = await self.c.plugin.crm.create_callback(lead_id, due.astimezone(timezone.utc), reason, requested_by)
            self.s.callback_id = self.s.callback_id or str(created.get("id") or "") or None
        self.s.callback_at = as_utc_iso(due)
        return due

    async def schedule_callback(self, a: CallbackArgs) -> ToolOutcome:
        due = parse_when(a.when)
        if due is None:
            return ToolOutcome({"error": "unclear_time", "instruction": "Ask for a day and a rough time."})
        if due < now_ist() + timedelta(minutes=5):
            due = now_ist() + timedelta(minutes=30)
        due = await self._callback(due, a.reason)
        self.s.action("schedule_callback")
        return ToolOutcome({"scheduled": True, "whenIst": spoken_time(due),
                            "note": "Calls are made between 09:00 and 21:00 IST."})

    async def request_human(self, a: HumanArgs) -> ToolOutcome:
        self.s.handover_reason = self.s.handover_reason or a.reason
        if a.reason not in self.s.escalations:
            self.s.escalations.append(a.reason)
        if a.question and a.question not in self.s.unanswered:
            self.s.unanswered.append(a.question)
        # VoiceLink exposes no transfer API, so escalation is always a scheduled expert callback plus
        # a handover in the CRM (see DECISIONS.md).
        when = None
        if not self.s.callback_at:
            due = await self._callback(now_ist() + timedelta(hours=1), f"Expert follow-up: {a.reason.lower()}",
                                       requested_by="AGENT")
            when = spoken_time(due)
        self.s.action("request_human")
        return ToolOutcome({"transfer": False, "expertCallback": when or "already scheduled",
                            "instruction": "Tell them a property expert will call them back."})

    async def send_whatsapp(self, a: WhatsAppArgs) -> ToolOutcome:
        if not a.customer_agreed:
            return ToolOutcome({"error": "no_consent", "instruction": "Ask first; send only if they agree."})
        self.s.whatsapp_consent = True
        if a.kind not in self.s.whatsapp_requests:
            self.s.whatsapp_requests.append(a.kind)
        self.s.action("send_whatsapp")
        return ToolOutcome({"recorded": True, "note": "It will arrive on WhatsApp shortly after this call."})

    async def mark_do_not_call(self, a: ReasonArgs) -> ToolOutcome:
        await self.c.mark_dnc(a.reason or "explicit request")
        return ToolOutcome({"done": True, "instruction": "Apologise briefly and say goodbye."},
                           end_call=True, end_reason="do_not_call")

    async def end_call(self, a: EndArgs) -> ToolOutcome:
        self.s.ended_by_agent = a.reason
        if a.reason == "NOT_INTERESTED":
            self.s.visit_declined = True
        return ToolOutcome({"ok": True}, end_call=True, end_reason=a.reason.lower(), skip_closing=True)

    async def save_requirements(self, a: SaveArgs) -> ToolOutcome:
        r = self.s.requirements
        patch: dict[str, Any] = {}
        if a.name:
            r.name = a.name
            patch["name"] = a.name
        if a.intent:
            r.intent = patch["intent"] = a.intent
        if a.budget_min_inr is not None:
            r.budget_min = patch["budgetMin"] = a.budget_min_inr
        if a.budget_max_inr is not None:
            r.budget_max = patch["budgetMax"] = a.budget_max_inr
        if a.bhk:
            r.bhk = patch["bhk"] = sorted(set(a.bhk))
        if a.locality:
            r.locality = patch["location"] = a.locality
        if a.project:
            project = self._project(a.project)
            if project:
                r.project_id, r.project_name = project["id"], project["name"]
                patch["projectId"] = project["id"]
        if a.property_type:
            r.property_type = patch["propertyType"] = a.property_type
        if a.possession:
            r.possession = patch["possessionPreference"] = a.possession
        if a.timeline_months is not None:
            r.timeline_months = a.timeline_months
            r.timeline_text = patch["possessionTimeline"] = f"within {a.timeline_months} months"
        if a.purpose:
            r.purpose = patch["purpose"] = a.purpose
        if a.readback:
            self.s.readbacks[a.readback] = self.s.readbacks.get(a.readback, 0) + 1
        for value in (a.budget_min_inr, a.budget_max_inr):
            if value:
                self.s.guard.evidence.add(value)  # the caller's own budget may be read back
        if patch and self.s.lead_id:
            self.c.background(self.c.plugin.crm.update_lead(self.s.lead_id, patch))
        return ToolOutcome({"saved": True, "stillUnknown": r.missing()})

    # ---------------------------------------------------------------- catalogue of tools

    def tools(self) -> list[Tool]:
        t = self._tool
        return [
            # The only source of facts. Projects, prices, sizes and availability are not looked up in
            # the CRM: the agent answers from the uploaded documents or says an expert will confirm.
            t("ask_knowledge", "The uploaded documents, the only source of facts: projects, locations, configurations, "
                               "prices, sizes, availability, possession, RERA, amenities, specifications, "
                               "payment plan, charges and FAQs.",
              _schema({"question": {**_STR, "description": "Short English keywords, e.g. 'clubhouse swimming pool'."},
                       "project": _PROJECT,
                       "doc_types": {"type": "array", "items": {"type": "string", "enum": DOC_TYPES}}}, ["question"]),
              KnowledgeArgs, self.ask_knowledge, timeout_s=1.2, filler="filler_knowledge"),
            t("get_visit_slots", "Free site-visit slots for a project (IST). Offer two or three.",
              _schema({"project": _PROJECT, "preferred_day": {**_STR, "description": "e.g. 'Saturday', 'kal', 'tomorrow'."}},
                      ["project"]), SlotArgs, self.get_visit_slots, filler="filler_slots"),
            t("book_site_visit", "Book an offered slot for the caller.",
              _schema({"project": _PROJECT, "slot_start": _SLOT, "unit": _STR}, ["project", "slot_start"]),
              BookArgs, self.book_site_visit, timeout_s=5.0, filler="filler_booking"),
            t("reschedule_visit", "Move the caller's existing visit to an offered slot.",
              _schema({"slot_start": _SLOT, "reason": _STR}, ["slot_start"]), RescheduleArgs, self.reschedule_visit,
              timeout_s=5.0, filler="filler_booking"),
            t("cancel_visit", "Cancel the caller's existing visit.", _schema({"reason": _STR}), ReasonArgs,
              self.cancel_visit, filler=None),
            t("confirm_visit", "Confirm the caller's existing visit.", _schema({}), NoArgs, self.confirm_visit,
              filler=None),
            t("schedule_callback", "Schedule a call back at the time the caller asked for.",
              _schema({"when": {**_STR, "description": "ISO date-time in IST, e.g. 2026-10-03T18:00:00+05:30."},
                       "reason": _STR}, ["when"]), CallbackArgs, self.schedule_callback, filler=None),
            t("request_human", "Hand the caller to a human property expert.",
              _schema({"reason": {"type": "string", "enum": ["CUSTOMER_ASKED", "NEGOTIATION", "LEGAL", "UNANSWERED"]},
                       "question": _STR}, ["reason"]), HumanArgs, self.request_human, filler=None),
            t("send_whatsapp", "Record that the caller agreed to receive the brochure or visit details on WhatsApp.",
              _schema({"kind": {"type": "string", "enum": ["BROCHURE", "VISIT_CONFIRMATION"]},
                       "customer_agreed": {"type": "boolean"}}, ["kind", "customer_agreed"]),
              WhatsAppArgs, self.send_whatsapp, filler=None),
            t("mark_do_not_call", "The caller asked not to be called again.", _schema({"reason": _STR}), ReasonArgs,
              self.mark_do_not_call, filler=None),
            t("end_call", "End the call politely after saying goodbye.",
              _schema({"reason": {"type": "string", "enum": ["COMPLETED", "CALLBACK_SCHEDULED", "NOT_INTERESTED",
                                                             "WRONG_NUMBER", "DO_NOT_CALL", "CUSTOMER_BUSY"]}}, ["reason"]),
              EndArgs, self.end_call, filler=None),
            t("save_requirements", "Record what the caller wants as soon as you learn it.",
              _schema({"name": _STR, "intent": {"type": "string", "enum": ["BUY", "RENT"]},
                       "budget_min_inr": _INT, "budget_max_inr": _INT, "bhk": {"type": "array", "items": _INT},
                       "locality": _STR, "project": _PROJECT,
                       "property_type": {"type": "string", "enum": PROPERTY_TYPES},
                       "possession": {"type": "string", "enum": ["READY", "UNDER_CONSTRUCTION", "ANY"]},
                       "timeline_months": _INT, "purpose": {"type": "string", "enum": ["SELF_USE", "INVESTMENT"]},
                       "readback": {"type": "string", "enum": ["confirmed", "corrected"]}}),
              SaveArgs, self.save_requirements, timeout_s=1.0, filler=None),
        ]
