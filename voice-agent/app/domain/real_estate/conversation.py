"""One call, from the real-estate side: who is calling, why, what to say first, what is off-limits,
and what goes back to the CRM when it ends."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from app.domain.base import CallerTurnAction, CallInfo, CallRecord, Tool
from app.config import cost_prices
from app.lang.languages import LANGS, Lang
from app.observability.cost import MeteredProvider
from app.llm.base import Message

from . import flows
from .extraction import Extraction, extract
from .guardrails import asks_for_human, escalation_topic, wants_dnc, wrong_number
from .money import caller_amounts
from .prompt import PROMPT_VERSION, system_prompt
from .scoring import ScoreInputs, lead_score, temperature
from .sensitive import asks_for_promise, mentions_identity
from .state import CALL_TYPES, CallState
from .tools import ToolBox

if TYPE_CHECKING:
    from .plugin import RealEstatePlugin

log = logging.getLogger(__name__)

_OPENINGS: dict[str, dict[str, str]] = {
    "OUTBOUND_NEW_LEAD": {
        "en": "Hello{name}, this is Riya from {builder}. {disclosure} You enquired about {topic} — is this a good time to talk?",
        "hi": "नमस्ते{name}, मैं रिया, {builder} से बोल रही हूँ। {disclosure} आपने {topic} के बारे में पूछताछ की थी — क्या अभी बात करने का सही समय है?",
        "mr": "नमस्कार{name}, मी रिया, {builder} कडून बोलतेय. {disclosure} तुम्ही {topic} बद्दल चौकशी केली होती — आत्ता बोलायला वेळ आहे का?",
    },
    "VISIT_REMINDER": {
        "en": "Hello, am I speaking with{name}? This is Riya from {builder}. {disclosure}",
        "hi": "नमस्ते, क्या मेरी बात{name} जी से हो रही है? मैं रिया, {builder} से। {disclosure}",
        "mr": "नमस्कार, मी{name} यांच्याशी बोलतेय का? मी रिया, {builder} कडून. {disclosure}",
    },
    "CALLBACK": {
        "en": "Hello{name}, this is Riya from {builder}, calling back as you asked. {disclosure} Is this a good time?",
        "hi": "नमस्ते{name}, मैं रिया, {builder} से। आपने कॉल बैक के लिए कहा था। {disclosure} क्या अभी बात कर सकते हैं?",
        "mr": "नमस्कार{name}, मी रिया, {builder} कडून. तुम्ही परत कॉल करायला सांगितलं होतं. {disclosure} आत्ता बोलू शकतो का?",
    },
    "RE_ENGAGEMENT": {
        "en": "Hello{name}, this is Riya from {builder}. {disclosure} We spoke earlier about your home search — do you have two minutes?",
        "hi": "नमस्ते{name}, मैं रिया, {builder} से। {disclosure} पहले हमने आपके घर की तलाश के बारे में बात की थी — क्या दो मिनट बात कर सकते हैं?",
        "mr": "नमस्कार{name}, मी रिया, {builder} कडून. {disclosure} आपण आधी तुमच्या घराच्या शोधाबद्दल बोललो होतो — दोन मिनिटं बोलू शकतो का?",
    },
}
_TOPIC_FALLBACK = {"en": "a home with us", "hi": "घर", "mr": "घर"}
_BOOKING = re.compile(r"(site\s*visit|visit\s+(?:karna|book|schedule)|dekhne\s+aana|dekhna\s+hai|"
                      r"विज़िट|विजिट|व्हिजिट|पाहायला\s+यायचं|જોવા\s+આવવું|વિઝિટ)", re.I)
# Riya asked whether they would like to visit, and a short yes to that.
_VISIT_QUESTION = re.compile(r"(visit|विज़िट|विजिट|व्हिजिट|देखने|पाहायला)[^?।]*(\?|चाहेंगे|चाहेंगी|करायची|करना है|करें(?=$|[\s,.!?।]))", re.I)
_YES = re.compile(r"^\W*(?:हाँ|हां|हा|जी|ji|haan|han|yes|yeah|ok|okay|ओके|ठीक|theek|sure|ज़रूर|जरूर|बिल्कुल|चलेगा|"
                  r"chalega|कर\s+(?:दीजिए|दो|दीजिये)|हो|hoy)(?=$|[\s,.!?।])", re.I)
# What the builder does not sell: everything here is residential apartments.
_COMMERCIAL = re.compile(r"(office|ऑफिस|ऑफ़िस|commercial|कमर्शियल|कमर्शल|shop|दुकान|शॉप|showroom|शोरूम|warehouse|"
                         r"गोदाम|godown|वेयरहाउस|retail|रिटेल|co-?working|को-?वर्किंग)", re.I)
_NOT_FLAT = re.compile(r"(villa|विला|व्हिला|bungalow|बंगला|row\s*house|रो\s*हाउस|plot|प्लॉट|प्लाट|land|ज़मीन|जमीन|जमीन)", re.I)

# The caller asks to hear something again: then a repeat is wanted.
_REPEAT = re.compile(r"(दोबारा|फिर\s*से|फिरसे|repeat|again|पुन्हा|परत\s+सांगा|एक\s*बार\s*और|समझ\s+नहीं\s+आया)", re.I)
# Common Devanagari spellings of the localities, so a caller saying "हिंजवड़ी" is understood.
_LOCALITY_SPELLINGS = {
    "hinjewadi": ("हिंजवडी", "हिंजेवाड़ी", "हिंजवाड़ी", "हिंजेवाडी", "हिंजवड़ी"), "wakad": ("वाकड", "वाकड़"),
    "baner": ("बाणेर", "बानेर"), "kharadi": ("खराडी", "खराड़ी"), "wagholi": ("वाघोली",),
    "kothrud": ("कोथरूड", "कोथरुड"), "undri": ("उंड्री", "उंद्री"), "ravet": ("रावेत", "रावेट"),
    "viman nagar": ("विमान नगर", "विमाननगर"), "hadapsar": ("हडपसर", "हड़पसर"),
}
# Project aliases too common to mean the project ("metro station").
_WEAK_ALIASES = {"metro", "one", "park", "towers", "heights", "valley", "grove", "residency", "residences"}


def _devanagari_only(settings) -> bool:
    """Gnani's voice reads Latin-letter words with an English accent, as if switching language."""
    return bool(getattr(settings, "devanagari_only_speech", False)) or getattr(settings, "tts_primary", "") == "gnani"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


# "Tell me the details first", "later", "not now": the caller is not ready to book.
_DEFER = re.compile(r"(पहले\s+(?:आप\s+)?(?:मुझे\s+)?(?:detail|डिटेल)|(?:details?|डिटेल्स?|जानकारी)\s+(?:बताइए|बताओ|बता\s+दो|"
                    r"दीजिए|दो|चाहिए)|pehle\s+details?|details?\s+(?:batao|bataiye|chahiye)|बाद\s+में|baad\s+mein|"
                    r"अभी\s+नहीं|abhi\s+nahi|not\s+now|later|first\s+(?:tell|give|share|send)|सोच\s+(?:के|कर)|"
                    r"नंतर|आत्ता\s+नको|आधी\s+माहिती)", re.I)
_QUESTION = re.compile(r"(\?|^(?:what|how|when|where|is|are|does|do|can|kya|kitna|kab|kaun|kahan|किती|काय|कधी|"
                       r"क्या|कितना|कब|શું|કેટલા|ક્યારે)\b)", re.I)


class RealEstateConversation:
    def __init__(self, plugin: "RealEstatePlugin", info: CallInfo):
        self.plugin = plugin
        self.info = info
        s = plugin.settings
        self.state = CallState(call_id=info.call_id, phone=info.caller_phone)
        record = plugin.outbound_record(info)
        language = None
        if record is not None:
            custom = record.custom
            self.state.request_id = record.request_id
            self.state.phone = record.phone or info.caller_phone
            self.state.call_type = custom.get("callType") if custom.get("callType") in CALL_TYPES else "OUTBOUND_NEW_LEAD"
            self.state.context = dict(custom.get("context") or {})
            self.state.lead = {"id": str(custom.get("leadId")), "name": self.state.context.get("leadName"),
                               "language": custom.get("language")}
            self.state.appointment_id = str(custom["appointmentId"]) if custom.get("appointmentId") else None
            self.state.callback_id = str(custom["callbackId"]) if custom.get("callbackId") else None
            language = custom.get("language")
            self._prefill(self.state.context.get("knownPreferences") or {})
            if self.state.context.get("projectId"):
                self.state.requirements.project_id = str(self.state.context["projectId"])
                self.state.requirements.project_name = self.state.context.get("projectName")
        elif info.direction == "outbound":
            self.state.call_type = "OUTBOUND_NEW_LEAD"
        default = s.default_outbound_language if self.state.call_type != "INBOUND" else s.default_inbound_language
        self.initial_language: Lang = info.language_hint or (language if language in LANGS else default)
        self.state.language = self.initial_language
        self.toolbox = ToolBox(self)
        self._tools: list[Tool] | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- set-up

    def _prefill(self, prefs: dict[str, Any]) -> None:
        r = self.state.requirements
        r.intent = prefs.get("intent") or r.intent
        r.budget_min = _int(prefs.get("budgetMin")) or r.budget_min
        r.budget_max = _int(prefs.get("budgetMax")) or r.budget_max
        bhk = prefs.get("bhk")
        if bhk:
            r.bhk = [int(float(b)) for b in (bhk if isinstance(bhk, list) else [bhk])]
        r.locality = prefs.get("location") or r.locality
        r.property_type = prefs.get("propertyType") or r.property_type
        r.purpose = prefs.get("purpose") if prefs.get("purpose") in ("SELF_USE", "INVESTMENT") else r.purpose
        r.possession = prefs.get("possessionPreference") or r.possession
        r.timeline_text = prefs.get("possessionTimeline") or r.timeline_text
        for amount in (r.budget_min, r.budget_max):
            if amount:
                self.state.guard.evidence.add(amount)

    async def start(self) -> None:
        """Resolve an inbound caller to a lead (the engine bounds this at 1.5 s)."""
        if self.state.call_type == "INBOUND" and self.state.phone:
            lead = await self.plugin.crm.find_or_create_lead(self.state.phone, self.initial_language)
            self.state.lead = lead
            if lead.get("language") in LANGS and self.info.language_hint is None:
                self.initial_language = lead["language"]
                self.state.language = self.initial_language
            self._prefill(lead)
            if lead.get("doNotCall"):
                log.info("call %s: inbound caller is on the do-not-call list; answering but never calling back",
                         self.info.call_id)
        self.advance()

    def advance(self) -> None:
        flows.advance(self.state)

    def background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- speaking

    def opening(self, lang: Lang) -> tuple[str, str | None]:
        if self.state.call_type == "INBOUND":
            return self.plugin.phrases.render("greeting_inbound", lang), "greeting_inbound"
        template = _OPENINGS[self.state.call_type].get(lang) or _OPENINGS[self.state.call_type]["en"]
        name = self.state.lead_name
        topic = self.state.context.get("projectName") or self.state.requirements.locality or _TOPIC_FALLBACK[lang]
        text = template.format(name=f" {name}" if name else "", builder=self.plugin.phrases.builder_name,
                               disclosure=self.plugin.phrases.disclosure.get(lang, ""), topic=topic)
        return " ".join(text.split()), None

    def messages(self, lang: Lang) -> list[Message]:
        self.state.language = lang
        return [Message("system", system_prompt(self.state, self.plugin.phrases.builder_name, lang,
                                                self.plugin.settings.disclose_ai,
                                                _devanagari_only(self.plugin.settings)))]

    def tools(self) -> list[Tool]:
        if self._tools is None:
            self._tools = self.toolbox.tools()
        return self._tools

    def screen_caller(self, text: str, lang: Lang) -> CallerTurnAction | None:
        s = self.state
        s.language = lang
        s.caller_turns += 1
        s.guard.add_caller_text(text)  # the caller's own figures (their budget) may be read back
        s.last_caller_text = text
        if _COMMERCIAL.search(text):
            s.not_sold = "commercial"
        elif _NOT_FLAT.search(text) and s.not_sold is None:
            s.not_sold = "not_flat"
        self._dropped = ""
        s.caller_asked_repeat = bool(_REPEAT.search(text))
        mentioned = self.mentioned_project(text)
        if mentioned is not None:
            s.focus_project_id, s.focus_project_name = str(mentioned["id"]), mentioned["name"]
        self._check_budget(text)
        if wants_dnc(text):
            self.background(self.mark_dnc("explicit request on the call"))
            self.advance()
            return CallerTurnAction(phrase="dnc_ack", end_call=True, end_reason="do_not_call")
        if wrong_number(text):
            s.wrong_number = True
            self.background(self.mark_dnc("wrong number"))
            return CallerTurnAction(phrase="wrong_number", end_call=True, end_reason="wrong_number")
        if mentions_identity(text):
            s.action("refused_identity_data")
            return CallerTurnAction(phrase="sensitive_refusal")
        if asks_for_promise(text):
            if text not in s.unanswered:
                s.unanswered.append(text[:300])
            s.handover_reason = s.handover_reason or "UNANSWERED"
            s.action("declined_promise")
            return CallerTurnAction(phrase="no_promises")
        if asks_for_human(text):
            s.handover_reason = s.handover_reason or "CUSTOMER_ASKED"
            if "CUSTOMER_ASKED" not in s.escalations:
                s.escalations.append("CUSTOMER_ASKED")
        topic = escalation_topic(text)
        if topic:
            s.handover_reason = s.handover_reason or topic
            if topic not in s.escalations:
                s.escalations.append(topic)
        # A visit is wanted when the caller asks for one, or says yes to Riya's question about it.
        s.wants_visit_now = bool(_BOOKING.search(text)) or (s.visit_question_asked and bool(_YES.search(text))
                                                            and not _DEFER.search(text))
        s.visit_question_asked = False
        if s.wants_visit_now:
            s.asked_to_book = True
            s.visit_deferred = False
        elif _DEFER.search(text):
            s.visit_deferred = True
        if _QUESTION.search(text.strip()) and len(s.questions) < 50:
            s.questions.append(text.strip()[:300])
        if s.call_type == "VISIT_REMINDER" and s.caller_turns == 1:
            s.identity_confirmed = True
        self.advance()
        return None

    def take_dropped(self) -> str:
        """A repeat dropped from this answer. Spoken anyway when nothing else was said: a repeat is
        better than silence (which only triggers "I couldn't hear you")."""
        dropped, self._dropped = getattr(self, "_dropped", ""), ""
        return dropped

    def mentioned_project(self, text: str) -> dict[str, Any] | None:
        """The one project the caller's words name, by project name or by a locality with one project."""
        catalog = self.plugin.catalog or {}
        projects = catalog.get("projects", [])
        lowered = (text or "").lower()
        hits = []
        for p in projects:
            names = [p["name"].lower()] + [a for a in p.get("aliases", []) if a not in _WEAK_ALIASES and len(a) >= 5]
            if any(n and n in lowered for n in names):
                hits.append(p)
        if not hits:
            localities = {loc["id"]: loc["name"].lower() for loc in catalog.get("localities", [])}
            for p in projects:
                name = localities.get(p.get("localityId"), "")
                spellings = (name,) + _LOCALITY_SPELLINGS.get(name.split(" phase")[0], ())
                if name and any(sp and sp in lowered for sp in spellings):
                    hits.append(p)
        return hits[0] if len(hits) == 1 else None

    def _check_budget(self, text: str) -> None:
        """A budget far outside our price range is most likely misheard ("सत्तर से अस्सी" -> "780 लाख"):
        Riya confirms it once before searching. A figure the caller confirms is accepted."""
        s = self.state
        if s.budget_to_confirm is not None:
            s.budgets_confirmed.append(s.budget_to_confirm)  # they have answered the confirmation
            s.budget_to_confirm = None
        prices = [t for p in (self.plugin.catalog or {}).get("projects", []) for t in p.get("unitTypes", [])]
        if not prices:
            return
        low = min(float(t["priceMinInr"]) for t in prices)
        high = max(float(t["priceMaxInr"]) for t in prices)
        for amount in caller_amounts(text):
            if amount in s.budgets_confirmed:
                continue
            if amount > high * 2 or amount < low * 0.5:
                s.budget_to_confirm = amount
                return

    def screen_reply(self, sentence: str, lang: Lang) -> str:
        s = self.state
        norm = _norm(sentence)
        visit_question = bool(_VISIT_QUESTION.search(sentence))
        visit_in_progress = s.wants_visit_now or s.asked_to_book or bool(s.slots_offered_for) or bool(s.booked_visit)
        if visit_question and s.visit_offers >= 1 and not visit_in_progress:
            # A site visit is optional: asked about once; never pressed again unless the caller raises it.
            log.info("call %s: dropped a repeated visit question", self.info.call_id)
            return ""
        if norm and norm in s.spoken and not s.caller_asked_repeat:
            log.info("call %s: dropped a sentence already said", self.info.call_id)
            self._dropped = self._dropped or sentence
            return ""
        unsupported = self.state.guard.unsupported(sentence)
        if unsupported:
            log.warning("call %s: fact guard replaced a sentence quoting unsupported figures %s "
                        "(sentence %r; amounts in evidence %s)", self.info.call_id, unsupported,
                        sentence[:160], sorted(self.state.guard.evidence)[:20])
            self.state.action("price_guard")
            return self.plugin.phrases.render("expert_confirm", lang)
        if norm:
            s.spoken.append(norm)
        return sentence

    def on_agent_reply(self, text: str) -> None:
        if _VISIT_QUESTION.search(text or ""):
            self.state.visit_question_asked = True
            self.state.visit_offers += 1
        self.advance()

    # ---------------------------------------------------------------- do-not-call

    async def mark_dnc(self, basis: str) -> None:
        s = self.state
        s.dnc = True
        s.dnc_basis = basis
        if s.phone:
            self.plugin.services.registry.suppress(s.phone, basis)  # the local cache, at once
            try:
                await asyncio.wait_for(self.plugin.crm.mark_dnc(s.phone, basis, s.lead_id), 3.0)
            except Exception as exc:  # noqa: BLE001 - the call record carries doNotCall too
                log.warning("call %s: CRM do-not-call update deferred to the call record: %r", self.info.call_id, exc)

    # ---------------------------------------------------------------- after the call

    def score(self, duration_s: float, extraction: Extraction) -> int:
        s, r = self.state, self.state.requirements
        months = r.timeline_months if r.timeline_months is not None else extraction.timeline_months
        return lead_score(ScoreInputs(
            budget_known=bool(r.budget_max or r.budget_min or extraction.budget_max_inr or extraction.budget_min_inr),
            budget_fits=s.budget_fits,
            bhk_known=bool(r.bhk or extraction.bhk),
            location_known=bool(r.locality or r.project_id or extraction.location),
            timeline_months=months,
            ready_to_move=(r.possession or extraction.possession_preference) == "READY",
            visit_booked=bool(s.booked_visit) or s.visit_outcome in ("CONFIRMED", "RESCHEDULED"),
            self_use=(r.purpose or extraction.purpose) == "SELF_USE",
            duration_s=duration_s))

    def _summary(self, extraction: Extraction) -> str:
        if extraction.summary:
            return extraction.summary
        s, r = self.state, self.state.requirements
        parts = [f"{s.call_type.replace('_', ' ').title()} call."]
        wants = ", ".join(f"{k}: {v}" for k, v in r.known().items() if k not in ("project_id",))
        if wants:
            parts.append(f"Requirements — {wants}.")
        if s.booked_visit:
            parts.append(f"Site visit booked at {s.booked_visit.get('projectName')} on {s.booked_visit.get('spokenTime')}.")
        if s.visit_outcome:
            parts.append(f"Visit {s.visit_outcome.lower()}.")
        if s.callback_at:
            parts.append(f"Callback requested for {s.callback_at}.")
        if s.unanswered:
            parts.append("Open questions: " + "; ".join(s.unanswered[:3]) + ".")
        if s.dnc:
            parts.append("Caller asked not to be called again.")
        return " ".join(parts)

    def build_record(self, record: CallRecord, extraction: Extraction) -> dict[str, Any]:
        s, r = self.state, self.state.requirements
        score = self.score(record.duration_s, extraction)
        temp = temperature(score)
        hot = temp == "HOT" and (bool(s.booked_visit) or s.asked_to_book)
        reason = s.handover_reason or ("HOT_LEAD" if hot else None)
        if reason not in ("CUSTOMER_ASKED", "NEGOTIATION", "LEGAL", "HOT_LEAD", "UNANSWERED"):
            reason = "UNANSWERED" if reason else None
        handover = bool(reason) or hot
        questions = list(dict.fromkeys(s.questions + extraction.questions_asked))[:50]
        unanswered = list(dict.fromkeys(s.unanswered + extraction.unanswered_questions))[:50]
        project_id = r.project_id or (str(s.context.get("projectId")) if s.context.get("projectId") else None)
        visit = s.booked_visit
        data: dict[str, Any] = {
            "callId": self.info.call_id[:80],
            "voiceSessionId": (s.request_id or self.info.call_id)[:80],
            "leadId": s.lead_id,
            "projectId": project_id,
            "customerPhone": s.phone,
            "direction": "outbound" if (s.request_id or self.info.direction == "outbound") else "inbound",
            "durationSeconds": round(record.duration_s, 1),
            "language": record.final_language if record.final_language in LANGS else None,
            "languagesUsed": [lang for lang in record.languages_used if lang in LANGS][:8],
            "customerName": s.lead_name or extraction.customer_name,
            "budget": r.budget_max or r.budget_min or extraction.budget_max_inr or extraction.budget_min_inr,
            "propertyType": r.property_type or extraction.property_type,
            "bhk": (r.bhk or extraction.bhk)[:8],
            "location": r.locality or r.project_name or extraction.location,
            "timeline": (r.timeline_text or extraction.timeline or "")[:80] or None,
            "intent": r.intent or extraction.intent,
            "leadScore": score,
            "siteVisit": f"BOOKED {visit.get('spokenTime')}"[:80] if visit else None,
            "customerSentiment": extraction.sentiment,
            "summary": self._summary(extraction),
            "questionsAsked": questions,
            "unansweredQuestions": unanswered,
            "agentActions": s.agent_actions[:50],
            "readbackOutcomes": {k: v for k, v in s.readbacks.items() if v},
            "doNotCall": True if s.dnc else None,
            "doNotCallBasis": s.dnc_basis,
            "transcript": record.transcript[-2000:],
            "callType": s.call_type,
            "appointmentId": s.appointment_id or (visit or {}).get("id"),
            "callbackId": s.callback_id if s.call_type == "CALLBACK" else None,
            "visitOutcome": s.visit_outcome,
            "callbackAt": s.callback_at,
            "handoverRequested": True if handover else None,
            "handoverReason": reason,
            "leadTemperature": temp,
            "purpose": r.purpose or extraction.purpose,
            "possessionPreference": r.possession or extraction.possession_preference,
            "whatsappConsent": s.whatsapp_consent,
            "whatsappRequests": s.whatsapp_requests,
            "citations": s.citations[:20],
            "promptVersion": PROMPT_VERSION,
        }
        if record.metrics.get("turns", 0) == 0 and s.caller_turns == 0:
            data["failureReason"] = "NO_RESPONSE" if record.end_reason != "caller_hangup" else "CALLER_HUNG_UP"
        return {k: v for k, v in data.items() if v not in (None, [], {})}

    async def finish(self, record: CallRecord) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=5)
        extraction = Extraction()
        if self.state.caller_turns > 0:
            provider = self.plugin.services.router.primary
            if record.cost is not None:
                provider = MeteredProvider(provider, record.cost, "post_call")
            extraction = await extract(provider, record.transcript, self.state.tool_log)
        payload = self.build_record(record, extraction)
        log.info("call %s finished: %s, score %s, prompt %s", self.info.call_id, self.state.call_type,
                 payload.get("leadScore"), PROMPT_VERSION)
        self.plugin.enqueue_ingest(payload)
        if self.state.request_id:
            self.plugin.services.registry.update(self.state.request_id, details={
                "summary": payload.get("summary"), "outcome": payload.get("visitOutcome") or payload.get("leadTemperature"),
                "leadScore": payload.get("leadScore"), "callType": self.state.call_type,
                "transcript": "\n".join(f"{t.get('speaker')}: {t.get('text')}" for t in record.transcript),
                "requirements": self.state.requirements.known(),
                "handoverStatus": "PENDING" if payload.get("handoverRequested") else "NOT_REQUESTED",
                "callbackStatus": "REQUESTED" if payload.get("callbackAt") else "NOT_REQUESTED",
                "cost": record.cost.summary(cost_prices(self.plugin.settings)) if record.cost is not None else None})


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
