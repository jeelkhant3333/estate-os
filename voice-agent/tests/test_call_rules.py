"""The must-follow rules: no repeats, a visit asked about once, visits only for the caller's project,
and a far-off budget confirmed before searching."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.domain.base import CallInfo
from app.domain.real_estate.plugin import RealEstatePlugin
from app.domain.real_estate.tools import SlotArgs
from app.outbound.registry import CallRegistry


def _conversation():
    settings = Settings(crm_mode="mock")
    services = SimpleNamespace(settings=settings, outbox=SimpleNamespace(register=lambda *a: None),
                               dialer=SimpleNamespace(), registry=CallRegistry(Path(tempfile.mkdtemp()) / "c.sqlite"))
    plugin = RealEstatePlugin(services)
    asyncio.run(plugin.refresh_catalog())
    return plugin.conversation(CallInfo("t", "outbound", "+919800000001", "hi"))


def test_a_sentence_already_said_is_not_said_again():
    c = _conversation()
    c.screen_caller("मुझे 2 BHK चाहिए", "hi")
    assert c.screen_reply("आप किस area में देख रहे हैं?", "hi")
    assert c.screen_reply("आप किस area में देख रहे हैं?", "hi") == ""
    c.screen_caller("दोबारा बताइए", "hi")  # asked to repeat: allowed
    assert c.screen_reply("आप किस area में देख रहे हैं?", "hi")


def test_the_visit_is_asked_about_once():
    c = _conversation()
    c.screen_caller("Baner में क्या है?", "hi")
    first = "क्या आप Sahyadri Grove की site visit करना चाहेंगे?"
    assert c.screen_reply(first, "hi")
    c.on_agent_reply(first)
    c.screen_caller("details बताइए", "hi")
    assert c.screen_reply("तो क्या आप visit करना चाहेंगे?", "hi") == ""
    c.screen_caller("ठीक है site visit book कर दो", "hi")  # the caller raises it: allowed
    assert c.screen_reply("कौन सा visit time ठीक रहेगा?", "hi")


def test_visit_slots_only_for_the_project_the_caller_is_talking_about():
    c = _conversation()
    c.screen_caller("Baner वाला project बताइए, visit करना है", "hi")
    other = asyncio.run(c.toolbox.get_visit_slots(SlotArgs(project="Mula Vista"))).content
    assert other["error"] == "not_the_callers_project" and other["callerIsAskingAbout"] == "Sahyadri Grove"
    same = asyncio.run(c.toolbox.get_visit_slots(SlotArgs(project="Sahyadri Grove"))).content
    assert "error" not in same


def test_a_far_off_budget_is_confirmed_before_searching():
    c = _conversation()
    c.screen_caller("मेरा budget 780 लाख है", "hi")
    assert c.state.budget_to_confirm == 78_000_000
    from app.domain.real_estate.prompt import system_prompt
    assert "confirm it once" in system_prompt(c.state, "XYZ Realty", "hi")
    c.screen_caller("नहीं नहीं, 70 से 80 लाख", "hi")  # the caller corrects it
    assert c.state.budget_to_confirm is None


def test_a_confirmed_unusual_budget_is_accepted():
    c = _conversation()
    c.screen_caller("budget 780 लाख", "hi")
    c.screen_caller("हाँ, 780 लाख ही है", "hi")
    assert c.state.budget_to_confirm is None


def test_a_reply_that_is_only_a_repeat_is_said_rather_than_silence():
    c = _conversation()
    c.screen_caller("Baner में क्या है?", "hi")
    assert c.screen_reply("Sahyadri Grove में 2 BHK available है।", "hi")
    c.screen_caller("हम्म", "hi")
    assert c.screen_reply("Sahyadri Grove में 2 BHK available है।", "hi") == ""
    assert c.take_dropped() == "Sahyadri Grove में 2 BHK available है।"
    assert c.take_dropped() == ""


def test_an_office_request_is_answered_with_what_we_sell():
    from app.domain.real_estate.prompt import system_prompt
    c = _conversation()
    c.screen_caller("मुझे कॉर्पोरेट ऑफिस में investment करना है", "hi")
    assert c.state.not_sold == "commercial"
    prompt = system_prompt(c.state, "XYZ Realty", "hi")
    assert "only residential apartments" in prompt and "Do not ask about BHK" in prompt


def test_a_villa_request_is_told_we_have_flats_first():
    c = _conversation()
    c.screen_caller("मुझे विला बाय करना है", "hi")
    assert c.state.not_sold == "not_flat"


def test_with_the_gnani_voice_riya_writes_everything_in_devanagari():
    from app.domain.real_estate.prompt import language_rule
    assert "entirely in Devanagari" in language_rule("hi", devanagari_only=True)
    assert "may stay English" in language_rule("hi")


def test_a_thinking_sound_is_not_an_answer():
    from app.conversation.interruption import is_hesitation
    for sound in ("हम", "उम्", "हम्म", "हम्म... अ", "hmm", "umm", "अं"):
        assert is_hesitation(sound), sound
    for answer in ("हाँ", "2 BHK", "हम बाणेर में देख रहे हैं", "जी", "ok"):
        assert not is_hesitation(answer), answer
