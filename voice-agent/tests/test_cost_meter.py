"""Per-call cost: the meter's arithmetic, and that a session actually fills it."""

import asyncio

from app.llm.base import Usage
from app.llm.fake import ScriptedLLM, say
from app.observability.cost import CostMeter, MeteredProvider, Prices

from tests.helpers import StubConversation, session


def test_jeels_call_costs_what_was_measured_by_hand():
    m = CostMeter(stt_seconds=172.53, tts_chars=1443)
    m.add_usage("call", Usage(56112, 726)); m.llm["call"].requests = 16
    for _ in range(6):
        m.add_usage("probe", Usage(201, 8))
    m.add_usage("post_call", Usage(1769, 249))
    s = m.summary(Prices())
    assert s["stt"]["cost"] == 1.4378 and s["tts"]["cost"] == 4.329
    assert round(s["llm"]["call"]["cost"], 3) == 1.696 and round(s["llm"]["post_call"]["cost"], 3) == 0.070
    assert round(s["during_call"], 2) == 7.50 and round(s["per_minute"], 2) == 2.61


def test_cached_input_is_billed_at_the_cached_rate():
    m = CostMeter()
    m.add_usage("call", Usage(1_000_000, 0, cached_tokens=1_000_000))
    assert m.summary(Prices())["llm"]["call"]["cost"] == 10.98


def test_probe_shares_split_between_calls():
    a, b = CostMeter(), CostMeter()
    for meter in (a, b):
        meter.add_request("probe", 0.5)
        meter.add_usage("probe", Usage(200, 8), 0.5)
    assert a.llm["probe"].prompt_tokens == 100 and a.llm["probe"].requests == 0.5


def test_a_session_records_model_usage_per_request():
    llm = ScriptedLLM([say("नमस्ते।") + [Usage(3500, 40)]])
    s, _ = session(llm, StubConversation())
    asyncio.run(s.answer("hello", "hi"))
    u = s.cost.llm["call"]
    assert u.requests == 1 and u.prompt_tokens == 3500 and u.completion_tokens == 40


def test_metered_provider_counts_post_call_usage():
    meter = CostMeter()
    provider = MeteredProvider(ScriptedLLM([say("{}") + [Usage(1769, 249)]]), meter, "post_call")

    async def run():
        async for _ in provider.stream([], []):
            pass
    asyncio.run(run())
    assert meter.llm["post_call"].requests == 1 and meter.llm["post_call"].prompt_tokens == 1769


def test_a_probe_reports_its_usage_for_the_live_calls():
    from app.resilience.degradation import LegState
    from app.resilience.probes import LLMProbe

    class Leg:
        state = LegState.HEALTHY

        def __init__(self):
            self.samples = []

        def needs_probe_samples(self, now):
            return True

        def record(self, *a, **kw):
            self.samples.append(kw)

    seen = []
    probe = LLMProbe(ScriptedLLM([say("ok") + [Usage(201, 8)]]), Leg(), active_calls=lambda: 1,
                     on_usage=seen.append)
    assert asyncio.run(probe.run_once()) is True
    assert seen and seen[0].prompt_tokens == 201 and seen[0].completion_tokens == 8


def test_prices_come_from_settings():
    """The path the call-end code takes (a misplaced cache decorator once broke it on a live call)."""
    from app.config import Settings, cost_prices
    p = cost_prices(Settings(cost_tts_per_1k_chars=2.5))
    assert p.tts_per_1k_chars == 2.5 and p.llm_input_per_m == 29.28 and p.currency == "INR"
