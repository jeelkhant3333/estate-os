"""Replays Jeel's call: times offered, asked again, then booked. The booking must succeed."""

import asyncio
from types import SimpleNamespace

from app.crm.mock import MockCrm
from app.domain.real_estate.state import CallState
from app.domain.real_estate.tools import BookArgs, SlotArgs, ToolBox


def _toolbox():
    crm = MockCrm(1)
    catalog = asyncio.run(crm.get_catalog())
    projects = {p["name"].lower(): p for p in catalog["projects"]}
    plugin = SimpleNamespace(crm=crm, catalog=catalog,
                             resolve_project=lambda name: projects.get(name.lower()))
    state = CallState(call_type="OUTBOUND_NEW_LEAD", phone="+919800000001")
    state.wants_visit_now = True  # the caller has asked for a visit
    return ToolBox(SimpleNamespace(plugin=plugin, state=state)), state


def test_a_repeated_slot_request_still_carries_bookable_slots():
    box, state = _toolbox()
    first = asyncio.run(box.get_visit_slots(SlotArgs(project="Sahyadri Grove"))).content
    assert first["slots"]
    state.wants_visit_now = False  # a later turn that did not ask about the visit
    again = asyncio.run(box.get_visit_slots(SlotArgs(project="Sahyadri Grove"))).content
    assert "alreadyOffered" in again and again["alreadyOffered"][0]["slot_start"]
    booked = asyncio.run(box.book_site_visit(BookArgs(project="Sahyadri Grove",
                                                      slot_start=again["alreadyOffered"][0]["slot_start"]))).content
    assert booked.get("booked") is True and state.booked_visit


def test_booking_by_the_spoken_label_works():
    box, state = _toolbox()
    slots = asyncio.run(box.get_visit_slots(SlotArgs(project="Sahyadri Grove"))).content["slots"]
    booked = asyncio.run(box.book_site_visit(BookArgs(project="Sahyadri Grove", slot_start=slots[0]["label"]))).content
    assert booked.get("booked") is True


def test_a_wrong_slot_comes_back_with_the_valid_ones():
    box, _ = _toolbox()
    asyncio.run(box.get_visit_slots(SlotArgs(project="Sahyadri Grove")))
    refused = asyncio.run(box.book_site_visit(BookArgs(project="Sahyadri Grove", slot_start="2030-01-01T10:00:00Z"))).content
    assert refused["error"] == "slot_not_offered" and refused["offered"][0]["slot_start"]


def test_show_me_a_project_does_not_fetch_visit_times():
    box, state = _toolbox()
    state.wants_visit_now = False  # "Baner वाले में दिखाइए": about the project, not a visit
    refused = asyncio.run(box.get_visit_slots(SlotArgs(project="Sahyadri Grove"))).content
    assert "slots" not in refused and refused["visitTimesLookedUp"] is False
    assert "Never say no slots are available" in refused["instruction"]
    assert state.offered_slots == {}
