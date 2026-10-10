"""Gnani experiment adapters against a local stand-in for api.vachana.ai."""

import asyncio
import base64
import json

import pytest
from websockets.asyncio.server import serve

from app.llm.base import Message
from app.llm.openai_compat import OpenAICompatLLM
from app.stt.gnani import GnaniSTT
from app.tts.gnani import GnaniTTS


async def test_stt_sends_pcm_frames_and_turns_segments_into_finals():
    received: list[bytes] = []
    headers = {}

    async def server(ws):
        headers.update(dict(ws.request.headers))
        await ws.send(json.dumps({"type": "connected"}))
        async for msg in ws:
            received.append(msg)
            if sum(len(m) for m in received) >= 2048:
                await ws.send(json.dumps({"type": "processing"}))
                await ws.send(json.dumps({"type": "transcript", "text": "मुझे 2 BHK चाहिए"}))
                break

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stt = GnaniSTT("k", "hi-IN", url=f"ws://127.0.0.1:{port}")
        await stt.start()
        await stt.send_audio(b"\xff" * 1024)  # 1,024 μ-law bytes -> 2,048 PCM bytes -> two frames
        events = []
        async for e in stt.events():
            events.append(e)
            if e.kind == "final":
                break
        await stt.close()
    assert headers["lang_code"] == "hi-IN" and headers["x-sample-rate"] == "8000"
    assert [len(m) for m in received] == [1024, 1024]
    assert [(e.kind, e.text) for e in events] == [("speech_end", ""), ("final", "मुझे 2 BHK चाहिए")]


async def test_tts_asks_for_phone_audio_and_reuses_one_connection():
    requests = []
    connections = 0

    async def server(ws):
        nonlocal connections
        connections += 1
        async for msg in ws:
            requests.append(json.loads(msg))
            await ws.send(json.dumps({"type": "start"}))
            await ws.send(json.dumps({"type": "audio", "data": {"audio": base64.b64encode(b"\x7f" * 160).decode()}}))
            await ws.send(json.dumps({"type": "complete", "data": {"audio": "", "is_final": True}}))
            await ws.send(json.dumps({"type": "complete", "message": "Streaming completed"}))

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        tts = GnaniTTS("k", {"hi": "Nalini", "mr": "Zahira"}, url=f"ws://127.0.0.1:{port}")
        first = b"".join([c async for c in tts.synthesize("नमस्ते", "hi")])
        second = b"".join([c async for c in tts.synthesize("नमस्कार", "mr")])
        await tts.close()
    assert first == second == b"\x7f" * 160
    assert connections == 1
    assert requests[0]["voice"] == "Nalini" and requests[0]["language"] == "hi-IN"
    assert requests[1]["voice"] == "Zahira" and requests[1]["language"] == "mr-IN"
    assert requests[0]["audio_config"]["encoding"] == "pcm_mulaw" and requests[0]["audio_config"]["sample_rate"] == 8000


def test_openai_compatible_llm_drops_sarvam_only_fields():
    llm = OpenAICompatLLM("gnani", "secret", "evon", "https://llm.example/v1/chat/completions", "X-API-Key-ID")
    body = llm.payload([Message("user", "hi")], [], 100, 0.3)
    assert "reasoning_effort" not in body and body["model"] == "evon"
    assert llm._url == "https://llm.example/v1/chat/completions"
    assert llm._headers["X-API-Key-ID"] == "secret"


async def test_a_sentence_after_the_first_is_not_silent():
    """Gnani closes each request with two "complete" messages; the second must not end the next one."""

    async def server(ws):
        async for msg in ws:
            n = len(json.loads(msg)["text"])
            await ws.send(json.dumps({"type": "start"}))
            await ws.send(json.dumps({"type": "audio", "data": {"audio": base64.b64encode(b"\x01" * n).decode()}}))
            await ws.send(json.dumps({"type": "complete", "data": {"audio": "", "is_final": True}}))
            await ws.send(json.dumps({"type": "complete", "message": "Streaming completed"}))

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        tts = GnaniTTS("k", {"hi": "Urmila"}, url=f"ws://127.0.0.1:{port}")
        sizes = []
        for text in ("एक", "दो दो", "तीन तीन तीन"):
            sizes.append(len(b"".join([c async for c in tts.synthesize(text, "hi")])))
        await tts.close()
    assert sizes == [len("एक"), len("दो दो"), len("तीन तीन तीन")]


async def test_a_barge_in_stop_is_not_a_tts_failure():
    """Closing the connection to stop Riya mid-sentence ends that sentence quietly; the next one works."""

    async def server(ws):
        async for msg in ws:
            await ws.send(json.dumps({"type": "start"}))
            await ws.send(json.dumps({"type": "audio", "data": {"audio": base64.b64encode(b"\x01" * 160).decode()}}))
            if "slow" in json.loads(msg)["text"]:
                await asyncio.sleep(5)
            await ws.send(json.dumps({"type": "complete", "message": "Streaming completed"}))

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        tts = GnaniTTS("k", {"hi": "Urmila"}, url=f"ws://127.0.0.1:{port}")
        got = []

        async def speak():
            async for chunk in tts.synthesize("slow sentence", "hi"):
                got.append(chunk)
                await tts.cancel()  # the caller starts talking

        await asyncio.wait_for(speak(), 2)
        after = b"".join([c async for c in tts.synthesize("next", "hi")])
        await tts.close()
    assert got == [b"\x01" * 160] and after == b"\x01" * 160
