"""A final that arrives after its turn was committed on partial text is not a second turn."""

import asyncio

from app.conversation.turn_detection import TurnConfig, TurnDetector

FAST = TurnConfig(base_silence_ms=20, incomplete_silence_ms=20, max_silence_ms=40, final_wait_ms=30)


def test_a_late_final_repeating_the_committed_turn_is_dropped():
    async def go():
        d = TurnDetector(FAST)
        d.on_speech_start()
        d.on_partial("कौन कौन से projects available हैं")
        d.on_speech_end()
        first = await asyncio.wait_for(d.turns.get(), 1)      # committed on the partial
        d.on_final("कौन कौन से projects available हैं?")        # Deepgram's final, late
        d.on_speech_start(); d.on_partial("Wakad"); d.on_speech_end()
        second = await asyncio.wait_for(d.turns.get(), 1)
        return first.text, second.text
    first, second = asyncio.run(go())
    assert "projects" in first and second == "Wakad"


def test_the_caller_repeating_themselves_is_still_a_turn():
    async def go():
        d = TurnDetector(FAST)
        texts = []
        for _ in range(2):  # two separate utterances, each with its own speech start
            d.on_speech_start(); d.on_final("हाँ जी"); d.on_speech_end()
            texts.append((await asyncio.wait_for(d.turns.get(), 1)).text)
        return texts
    assert asyncio.run(go()) == ["हाँ जी", "हाँ जी"]
