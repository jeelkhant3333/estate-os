"""The engine's tool-calling loop: call, filler, result back to the model, then the answer."""

import asyncio
import json

from app.domain.base import CallerTurnAction, Tool, ToolOutcome
from app.llm.base import ToolSpec
from app.llm.fake import ScriptedLLM, call, say

from tests.helpers import PHRASES, StubConversation, session


def _tool(run, filler="filler_lookup", timeout_s=1.0):
    return Tool(ToolSpec("lookup", "look something up",
                         {"type": "object", "properties": {"q": {"type": "string"}},
                          "required": ["q"], "additionalProperties": False}),
                run, timeout_s=timeout_s, filler=filler)


def test_a_tool_result_is_handed_back_before_the_answer():
    seen = []

    async def run(args):
        seen.append(args)
        return ToolOutcome({"price": "85 lakh"})

    llm = ScriptedLLM([call("lookup", q="2 bhk"), say("The two bedroom is 85 lakh.")])
    s, speech = session(llm, StubConversation(tool_list=[_tool(run)]))
    asyncio.run(s._respond("price of 2 bhk", "en"))
    assert seen == [{"q": "2 bhk"}]
    # The filler covers the lookup; then the answer.
    assert speech.started == [PHRASES["filler_lookup"], "The two bedroom is 85 lakh."]
    tool_msg = [m for m in llm.calls[1] if m.role == "tool"][0]
    assert json.loads(tool_msg.content) == {"price": "85 lakh"}


def test_a_slow_tool_times_out_and_the_model_is_told_not_to_guess():
    async def run(args):
        await asyncio.sleep(1)
        return ToolOutcome({"price": "late"})

    llm = ScriptedLLM([call("lookup", q="x"), say("I will have that confirmed.")])
    s, speech = session(llm, StubConversation(tool_list=[_tool(run, timeout_s=0.05)]))
    asyncio.run(s._respond("price", "en"))
    tool_msg = [m for m in llm.calls[1] if m.role == "tool"][0]
    assert json.loads(tool_msg.content)["error"] == "timeout"
    assert s.metrics.tool_errors == 1


def test_bad_arguments_and_unknown_tools_do_not_crash_the_call():
    async def run(args):
        return ToolOutcome({"ok": True})

    llm = ScriptedLLM([call("nope", q="x"), say("Sorry about that.")])
    s, speech = session(llm, StubConversation(tool_list=[_tool(run)]))
    asyncio.run(s._respond("hello", "en"))
    assert speech.started[-1] == "Sorry about that."
    assert s.metrics.tool_errors == 1


def test_tool_hops_are_bounded():
    async def run(args):
        return ToolOutcome({"again": True})

    loops = [call("lookup", q=str(i)) for i in range(10)]
    llm = ScriptedLLM(loops + [say("done")])
    s, _ = session(llm, StubConversation(tool_list=[_tool(run, filler=None)]), max_tool_hops=2)
    asyncio.run(s._respond("hello", "en"))
    # Two tool hops, then a final call offered no tools.
    assert len(llm.calls) == 3


def test_a_screened_turn_never_reaches_the_model():
    llm = ScriptedLLM()
    convo = StubConversation(screen=lambda text: CallerTurnAction(say="Understood.", end_call=True,
                                                                  end_reason="dnc"))
    s, speech = session(llm, convo)

    async def scenario():
        await s.turns.turns.put(type("T", (), {"text": "don't call me", "language": None})())
        await asyncio.wait_for(s._turn_loop(), 1)

    asyncio.run(scenario())
    assert llm.calls == []
    assert speech.started == ["Understood."]
    assert s.metrics.end_reason == "dnc"


def test_an_end_call_tool_hangs_up_after_the_reply():
    async def end(args):
        return ToolOutcome({"ok": True}, end_call=True, end_reason="agent_closed", skip_closing=True)

    tool = Tool(ToolSpec("end_call", "end", {"type": "object", "properties": {}}), end, filler=None)
    llm = ScriptedLLM([call("end_call"), say("Thank you, goodbye.")])
    hung_up = []
    s, speech = session(llm, StubConversation(tool_list=[tool]))
    s.hangup = lambda: hung_up.append(True)

    async def scenario():
        await s.turns.turns.put(type("T", (), {"text": "that's all for now", "language": None,
                                               "committed_at": 0, "speech_ended_at": 0})())
        await asyncio.wait_for(s._turn_loop(), 1)

    asyncio.run(scenario())
    assert speech.started == ["Thank you, goodbye."]
    assert hung_up and s.metrics.end_reason == "agent_closed"


class _ToolsSeenLLM(ScriptedLLM):
    """Records which tools each hop declared."""

    def __init__(self, steps):
        super().__init__(steps)
        self.tools_per_call: list[int] = []

    async def stream(self, messages, tools, **kw):
        self.tools_per_call.append(len(tools))
        async for event in super().stream(messages, tools, **kw):
            yield event


def test_end_call_repeated_instead_of_a_goodbye_still_closes_politely():
    """The model ended the call, then called end_call again instead of saying goodbye: the second call
    is ignored and the closing line is spoken, instead of a provider error and an apology."""
    ended = []

    async def end(args):
        ended.append(True)
        return ToolOutcome({"ok": True}, end_call=True, end_reason="agent_closed", skip_closing=True)

    tool = Tool(ToolSpec("end_call", "end", {"type": "object", "properties": {}}), end, filler=None)
    llm = _ToolsSeenLLM([call("end_call"), call("end_call"), say("should never be asked")])
    hung_up = []
    s, speech = session(llm, StubConversation(tool_list=[tool]))
    s.hangup = lambda: hung_up.append(True)

    async def scenario():
        await s.turns.turns.put(type("T", (), {"text": "ji", "language": None,
                                               "committed_at": 0, "speech_ended_at": 0})())
        await asyncio.wait_for(s._turn_loop(), 1)

    asyncio.run(scenario())
    assert ended == [True]                      # the second end_call was not run
    assert len(llm.calls) == 2                  # no third hop
    assert all(n == 1 for n in llm.tools_per_call)  # tools declared on every hop, including the last
    assert speech.started == [PHRASES["closing"]] and hung_up


def test_the_last_hop_still_declares_tools():
    async def run(args):
        return ToolOutcome({"ok": True})

    llm = _ToolsSeenLLM([call("lookup", q="a"), call("lookup", q="b"), call("lookup", q="c"),
                         call("lookup", q="d"), say("never")])
    s, _ = session(llm, StubConversation(tool_list=[_tool(run, filler=None)]))
    asyncio.run(s._respond("q", "en"))
    assert llm.tools_per_call and all(n == 1 for n in llm.tools_per_call)


def test_one_filler_per_turn_however_many_lookups():
    async def run(args):
        return ToolOutcome({"ok": True})

    llm = ScriptedLLM([call("lookup", q="a"), call("lookup", q="b"), say("Here it is.")])
    s, speech = session(llm, StubConversation(tool_list=[_tool(run)]))
    asyncio.run(s._respond("q", "en"))
    assert speech.started.count(PHRASES["filler_lookup"]) == 1


def test_the_callers_words_stop_riya_even_without_a_voice_onset():
    """Never talk over the caller: recognised words while Riya speaks interrupt her at once."""
    from app.conversation.events import PartialTranscript

    async def scenario():
        s, speech = session(ScriptedLLM([say("a long answer")]), StubConversation())
        speech._active = 1  # Riya is mid-sentence; the voice detector saw nothing (echo threshold)
        stopped = []

        async def interrupt():
            stopped.append(True)
        s._interrupt = interrupt
        s._words_while_speaking("नहीं रुकिए, मुझे 3 BHK चाहिए")
        await asyncio.sleep(0.05)
        s._words_while_speaking("हाँ")  # a backchannel alone would not have stopped her
        return stopped
    assert asyncio.run(scenario()) == [True]


def test_an_unexpected_value_never_throws_away_the_post_call_summary():
    from app.domain.real_estate.extraction import Extraction
    e = Extraction.model_validate({"intent": "INVESTMENT", "property_type": "corporate office", "sentiment": "curious",
                                   "summary": "wants an office"})
    assert (e.intent, e.property_type, e.sentiment, e.summary) == ("BUY", "COMMERCIAL", None, "wants an office")


def test_running_out_of_lookups_is_never_silence():
    """The model keeps searching an empty knowledge base: the caller hears a line, not dead air."""
    async def run(args):
        return ToolOutcome({"found": False})

    llm = ScriptedLLM([call("lookup", q=str(i)) for i in range(10)])
    s, speech = session(llm, StubConversation(tool_list=[_tool(run, filler=None)]), max_tool_hops=2)
    asyncio.run(s._respond("what are the amenities", "en"))
    assert speech.started == [PHRASES["no_answer"]]
    assert s.metrics.unanswered == 1
