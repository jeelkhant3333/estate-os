"""Gnani Vachana (Prisma) streaming STT — experiment.

wss://api.vachana.ai/stt/v3/stream with headers x-api-key-id, lang_code and x-sample-rate. Audio is
raw 16-bit PCM in 1,024-byte frames at real-time cadence (the phone's μ-law is converted here). The
server runs its own VAD and sends only whole segments: {"type": "processing"} when speech ends, then
{"type": "transcript", "text": ...}. There are no partial transcripts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.audio.codecs import mulaw_to_pcm16
from app.stt.base import STTEvent

log = logging.getLogger(__name__)

FRAME_BYTES = 1024  # 512 samples of 16-bit PCM


class GnaniSTT:
    URL = "wss://api.vachana.ai/stt/v3/stream"

    def __init__(self, api_key: str, language: str = "hi-IN", url: str = URL, min_silence_ms: int = 500,
                 vad_threshold: float = 0.7, bias_words: list[str] | None = None):
        self.name = "gnani_stt"
        self._api_key = api_key
        self._language = language
        self._url = url
        self._headers = {
            "x-api-key-id": api_key, "lang_code": language, "x-sample-rate": "8000",
            "x-min-silence-ms": str(min_silence_ms), "x-vad-threshold": str(vad_threshold),
        }
        self._bias = [w for w in (bias_words or []) if w.isalpha()][:100]
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._ws = None
        self._buffer = bytearray()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._ws = await connect(self._url, additional_headers=self._headers, open_timeout=5,
                                 ping_interval=20, ping_timeout=20)
        first = json.loads(await asyncio.wait_for(self._ws.recv(), 5))
        if first.get("type") == "error":
            raise RuntimeError(f"gnani stt: {first.get('message')}")
        if self._bias:
            await self._ws.send(json.dumps({"type": "update_settings", "bias": {"list": self._bias, "score": 1.5}}))
        self._task = asyncio.create_task(self._receive())

    async def send_audio(self, mulaw_8k: bytes) -> None:
        self._buffer.extend(mulaw_to_pcm16(mulaw_8k).tobytes())
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            await self._ws.send(frame)

    async def _receive(self) -> None:
        short = self._language.split("-")[0]
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "transcript":
                    text = (msg.get("text") or "").strip()
                    if text:
                        await self._queue.put(STTEvent("final", text, short))
                elif kind in ("speech_start", "vad_start"):
                    await self._queue.put(STTEvent("speech_start"))
                elif kind in ("processing", "speech_end", "vad_end"):
                    await self._queue.put(STTEvent("speech_end"))
                elif kind == "error":
                    await self._queue.put(STTEvent("error", msg.get("message", "error"), fatal=True))
        except ConnectionClosed as exc:
            if exc.rcvd is None or exc.rcvd.code != 1000:
                await self._queue.put(STTEvent("error", f"socket closed: {exc}", fatal=True))
        finally:
            await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
