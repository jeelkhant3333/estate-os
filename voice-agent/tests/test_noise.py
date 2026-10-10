"""Background noise neither interrupts Riya nor gets an answer."""

import asyncio

from app.conversation.interruption import BargeIn, BargeInDetector
from app.lang.languages import is_noise
from app.llm.fake import ScriptedLLM, say

from tests.helpers import StubConversation, session


def test_noise_transcripts():
    for noise in ("", "O", "A.", "अ", "અ ಆ ಸತ್ಯ"):
        assert is_noise(noise), noise
    # Speech in one other Indic script is Hindi spelt wrong: it is kept (and converted), not dropped.
    for speech in ("जी", "Hello", "Cool.", "2 BHK", "17 से ਅੱਸੀ ਲੱਖ ਮੈਂ", "हिंदी में बताइए", "ঠিক আছে।",
                   "ਸੱਠ ਤੋਂ ਸੱਤਰ ਲੱਖ", "ਜੀ"):
        assert not is_noise(speech), speech


def test_hindi_in_another_script_is_converted_to_devanagari():
    from app.lang.languages import to_devanagari_script
    assert to_devanagari_script("ਸੱਠ ਤੋਂ ਸੱਤਰ ਲੱਖ") == "सठ तों सतर लख"
    assert to_devanagari_script("ਜੀ") == "जी" and to_devanagari_script("2 BHK") == "2 BHK"


def test_a_noise_word_does_not_cut_riya_off():
    b = BargeInDetector(min_speech_ms=600)
    b.on_onset(0.0)
    b.on_text("O")
    assert b.evaluate(0.2) == BargeIn.PENDING   # judged on duration only
    b.on_text("नहीं रुकिए")
    assert b.evaluate(0.25) == BargeIn.INTERRUPT  # real words still interrupt at once


def test_a_noise_turn_gets_no_reply():
    llm = ScriptedLLM([say("should not be said")])
    s, speech = session(llm, StubConversation())

    async def scenario():
        await s.turns.turns.put(type("T", (), {"text": "અ ಆ ಸತ್ಯ", "language": None,
                                               "committed_at": 0, "speech_ended_at": 0})())
        try:
            await asyncio.wait_for(s._turn_loop(), 0.3)
        except asyncio.TimeoutError:
            pass

    asyncio.run(scenario())
    assert speech.started == [] and llm.calls == []
