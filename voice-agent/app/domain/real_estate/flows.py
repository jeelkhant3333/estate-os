"""Call flows as small stage machines.

The model chooses the words; this module decides what the call is for right now and when it may
move on. Each call type has its own stages. `advance()` is re-evaluated after every caller turn,
tool result and agent reply, and the current stage's goal is what the system prompt asks for.

  INBOUND / OUTBOUND_NEW_LEAD:  OPENING → DISCOVERY → QUALIFY → RECOMMEND → VISIT → WRAP_UP
  VISIT_REMINDER:               OPENING → REMIND → RESOLVE → WRAP_UP
  CALLBACK / RE_ENGAGEMENT:     OPENING → DELTA → QUALIFY → RECOMMEND → VISIT → WRAP_UP
"""

from __future__ import annotations

from .state import CallState

STAGES = {
    "INBOUND": ["OPENING", "DISCOVERY", "QUALIFY", "RECOMMEND", "VISIT", "WRAP_UP"],
    "OUTBOUND_NEW_LEAD": ["OPENING", "DISCOVERY", "QUALIFY", "RECOMMEND", "VISIT", "WRAP_UP"],
    "VISIT_REMINDER": ["OPENING", "REMIND", "RESOLVE", "WRAP_UP"],
    "CALLBACK": ["OPENING", "DELTA", "QUALIFY", "RECOMMEND", "VISIT", "WRAP_UP"],
    "RE_ENGAGEMENT": ["OPENING", "DELTA", "QUALIFY", "RECOMMEND", "VISIT", "WRAP_UP"],
}

GOALS = {
    ("INBOUND", "OPENING"): "Find out how you can help and which project or locality they are calling about "
                            "(an ad, hoarding or portal).",
    ("OUTBOUND_NEW_LEAD", "OPENING"): "Check it is a good time to talk. If not, offer a callback time "
                                      "(schedule_callback) and end the call politely.",
    ("CALLBACK", "OPENING"): "Confirm it is a good time, then pick up from the last conversation.",
    ("RE_ENGAGEMENT", "OPENING"): "Confirm it is a good time, then pick up from the last conversation.",
    ("VISIT_REMINDER", "OPENING"): "Confirm you are speaking to the right person, by name only. Ask for no ID.",
    "DISCOVERY": "Understand what they are looking for and which project or locality interests them.",
    "DELTA": "Ask what has changed since the last conversation and update only that.",
    "QUALIFY": "Fill the missing requirements naturally, one question per turn, never as an interrogation. "
               "Read critical values back once and record the outcome with save_requirements(readback=…).",
    "RECOMMEND": "Look up options in the documents (ask_knowledge) and recommend at most two that fit "
                 "their budget, as the documents describe them.",
    "VISIT": "A site visit is optional. If the caller sounds interested in a project, ask once whether they "
             "would like to see it; if they agree, get_visit_slots for that project, offer two or three times, "
             "book the one they pick with book_site_visit and confirm project, day, time and the agent's name. "
             "If they do not agree, drop it and answer their questions.",
    "REMIND": "Remind them of the visit: project, day and time, address and the agent meeting them.",
    "RESOLVE": "Confirm the visit (confirm_visit), or reschedule it (get_visit_slots, then reschedule_visit), "
               "or cancel it (ask why, offer a later date, cancel_visit).",
    "WRAP_UP": "Offer the brochure or visit details on WhatsApp (ask permission first; send_whatsapp only if "
               "they agree), summarise the next step in one sentence and end_call.",
}


# Once times are on the table, or the caller wants information first, stop selling the visit.
VISIT_OFFERED = ("You have already offered visit times. Do not read them again or ask again which one; answer "
                 "exactly what the caller asks (details, prices, amenities, location). Book with book_site_visit "
                 "only when they pick a time or ask to visit. If they are not ready, offer the details on WhatsApp "
                 "or a callback instead.")
VISIT_DEFERRED = ("The caller wants information before deciding on a visit. Answer their questions fully and "
                  "do not mention a site visit or times unless they bring it up.")


def goal(state: CallState) -> str:
    if state.stage == "VISIT" and not state.booked_visit:
        asked_without_yes = state.visit_offers >= 1 and not (state.asked_to_book or state.slots_offered_for)
        if state.visit_deferred or asked_without_yes:
            return VISIT_DEFERRED
        if state.slots_offered_for:
            return VISIT_OFFERED
    return GOALS.get((state.call_type, state.stage)) or GOALS.get(state.stage, "")


def _qualified(state: CallState) -> bool:
    r = state.requirements
    budget = r.budget_max is not None or r.budget_min is not None
    return budget and bool(r.bhk) and bool(r.locality or r.project_id)


def advance(state: CallState) -> str:
    """Move forward when the current stage's exit condition holds. Never moves back."""
    stages = STAGES.get(state.call_type, STAGES["INBOUND"])
    current = state.stage if state.stage in stages else stages[0]
    index = stages.index(current)

    def at_least(name: str) -> None:
        nonlocal index
        if name in stages:
            index = max(index, stages.index(name))

    if state.caller_turns >= 1:
        at_least(stages[1])
    if state.call_type == "VISIT_REMINDER":
        if state.identity_confirmed or state.caller_turns >= 1:
            at_least("REMIND")
        if state.caller_turns >= 2:
            at_least("RESOLVE")
        if state.visit_outcome:
            at_least("WRAP_UP")
    else:
        r = state.requirements
        if r.known() or state.recommended:
            at_least("QUALIFY")
        if _qualified(state) or state.recommended:
            at_least("RECOMMEND")
        if state.recommended or state.asked_to_book or state.offered_slots:
            at_least("VISIT")
        if state.booked_visit or state.visit_declined:
            at_least("WRAP_UP")
    if state.callback_at and state.caller_turns <= 2:
        at_least("WRAP_UP")  # "call me later": nothing more to do on this call
    state.stage = stages[index]
    return state.stage
