"""Riya offers a visit once and follows the caller: details first means details first."""

from app.domain.real_estate import flows
from app.domain.real_estate.conversation import _BOOKING, _DEFER
from app.domain.real_estate.state import CallState


def _visit_stage(**kw) -> CallState:
    s = CallState(call_type="OUTBOUND_NEW_LEAD")
    s.stage = "VISIT"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_first_the_visit_is_offered():
    assert "A site visit is optional" in flows.goal(_visit_stage())


def test_after_times_were_offered_riya_stops_selling():
    goal = flows.goal(_visit_stage(slots_offered_for=["11"]))
    assert goal == flows.VISIT_OFFERED and "Do not read them again" in goal


def test_details_first_means_no_visit_talk():
    assert flows.goal(_visit_stage(visit_deferred=True, slots_offered_for=["11"])) == flows.VISIT_DEFERRED


def test_what_jeel_said_is_recognised_as_details_first():
    for said in ("मुझे पहले आप detail बताइए", "आप मुझे details बताइए 2 BHK और 3 BHK की",
                 "अभी नहीं, बाद में", "first tell me the price"):
        assert _DEFER.search(said), said
    assert _BOOKING.search("site visit book कर दो")


def test_a_yes_to_rias_visit_question_counts_as_asking_for_a_visit():
    from app.domain.real_estate.conversation import _VISIT_QUESTION, _YES
    assert _VISIT_QUESTION.search("क्या आप site visit करना चाहेंगे?")
    assert _YES.search("हां जी कर दीजिए") and _YES.search("ok") and not _YES.search("नहीं, पहले details")
    assert not _VISIT_QUESTION.search("Greenleaf में clubhouse और gym है।")


def test_after_asking_once_without_a_yes_the_visit_is_dropped():
    assert flows.goal(_visit_stage(visit_offers=1)) == flows.VISIT_DEFERRED
    assert flows.goal(_visit_stage(visit_offers=1, asked_to_book=True)) != flows.VISIT_DEFERRED
