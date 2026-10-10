"""Turn timing: answers are spoken sentence by sentence, and the filler is only for slow answers.

Ported from the bank agent's session tests; the retrieval step those tests exercised is gone (the
engine no longer retrieves anything), the streaming and timing behaviour is unchanged.
"""

import asyncio

from app.llm.fake import ScriptedLLM, say

from tests.helpers import PHRASES, StubConversation, session

QUESTION = "what does the two bedroom cost"


def test_a_prompt_answer_is_spoken_without_the_filler():
    llm = ScriptedLLM([say("The two bedroom starts at 85 lakh. ",
                           "The three bedroom starts at 1.2 crore.")])
    s, speech = session(llm)
    asyncio.run(s._respond(QUESTION, "en"))
    assert PHRASES["thinking"] not in speech.started
    # Two sentences, spoken separately, and "1.2" is not split at its decimal point.
    assert speech.started == ["The two bedroom starts at 85 lakh.",
                              "The three bedroom starts at 1.2 crore."]


def test_the_filler_is_said_only_when_the_answer_is_slow():
    llm = ScriptedLLM([say("It starts at 85 lakh.")], first_event_delay_s=0.2)
    s, speech = session(llm, filler_after_ms=50)
    asyncio.run(s._respond(QUESTION, "en"))
    assert speech.started == [PHRASES["thinking"], "It starts at 85 lakh."]


def test_a_long_first_sentence_is_spoken_from_its_first_clause():
    llm = ScriptedLLM([say("For the two bedroom homes in the second tower facing the garden, ",
                           "the price starts at 85 lakh.")])
    s, speech = session(llm)
    asyncio.run(s._respond(QUESTION, "en"))
    assert speech.started == ["For the two bedroom homes in the second tower facing the garden,",
                              "the price starts at 85 lakh."]


def test_history_is_replayed_to_the_model_on_the_next_turn():
    llm = ScriptedLLM([say("We have two and three bedroom homes."), say("Yes, it has a pool.")])
    s, _ = session(llm)
    asyncio.run(s._respond("what sizes do you have", "en"))
    asyncio.run(s._respond("is there a pool", "en"))
    second_call = [m.content for m in llm.calls[1]]
    assert "what sizes do you have" in second_call
    assert "We have two and three bedroom homes." in second_call


def test_no_provider_means_the_safe_phrase():
    from app.llm.base import ProviderUnavailable

    llm = ScriptedLLM(error=ProviderUnavailable("down"))
    s, speech = session(llm)
    asyncio.run(s._respond(QUESTION, "en"))
    assert speech.started == [PHRASES["transfer"]]


def test_every_spoken_sentence_passes_the_reply_screen():
    llm = ScriptedLLM([say("It costs 99 lakh. Call me back.")])
    convo = StubConversation(screened_replies=lambda s: "[checked]" if "lakh" in s else s)
    s, speech = session(llm, convo)
    asyncio.run(s._respond(QUESTION, "en"))
    assert speech.started[0] == "[checked]"


class _Events:
    def __init__(self):
        self.events = asyncio.Queue()


def test_silence_prompts_stop_once_the_caller_speaks_again():
    """A prompt must not leave a timer behind that keeps prompting over the conversation."""
    from app.conversation.events import SpeechStarted

    async def scenario():
        s, speech = session(ScriptedLLM())
        s.caller = _Events()
        s.silence.first_prompt_s = s.silence.interval_s = 0.05
        s.silence.arm()
        loop = asyncio.create_task(s._event_loop())
        await asyncio.sleep(0.12)  # a quiet first tick, then one prompt
        await s.caller.events.put(SpeechStarted(0.0))
        await asyncio.sleep(0.3)
        loop.cancel()
        return speech.started

    started = asyncio.run(scenario())
    assert started == [PHRASES["still_there"]]


def test_silence_never_says_it_could_not_hear_the_caller():
    """The first quiet spell is waited out, not met with "sorry, I couldn't hear you"."""
    async def scenario():
        s, speech = session(ScriptedLLM())
        s.silence.first_prompt_s = s.silence.interval_s = 0.05
        s.silence.arm()
        await asyncio.sleep(0.07)  # first tick only
        s.silence.disarm()
        return speech.started

    assert asyncio.run(scenario()) == []
