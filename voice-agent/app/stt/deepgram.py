"""Deepgram Nova-3 streaming STT (backup, spec section 5).

Marathi calls use language=mr (monolingual); Hindi-family calls use language=multi.
Deepgram bills websocket connection time.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.stt.base import STTEvent


class DeepgramSTT:
    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "mr"):
        self.name = "deepgram_stt"
        self._api_key = api_key
        self._params = {
            "model": model, "language": language, "encoding": "mulaw", "sample_rate": 8000, "channels": 1,
            "interim_results": "true", "punctuate": "true", "vad_events": "true", "endpointing": 300,
        }
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._ws = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._ws = await connect(f"{self.URL}?{urlencode(self._params)}",
                                 additional_headers={"Authorization": f"Token {self._api_key}"}, open_timeout=5)
        self._tasks = [asyncio.create_task(self._receive()), asyncio.create_task(self._keepalive())]

    async def send_audio(self, mulaw_8k: bytes) -> None:
        await self._ws.send(mulaw_8k)

    async def flush(self) -> None:
        """The caller stopped talking: ask Deepgram to finalise what it has heard now, rather than
        after its own endpointing (finals otherwise arrived 1-1.5 s after speech end)."""
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "Finalize"}))

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def _receive(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "Results":
                    alternatives = (msg.get("channel") or {}).get("alternatives") or [{}]
                    text = alternatives[0].get("transcript", "")
                    if text:
                        await self._queue.put(STTEvent("final" if msg.get("is_final") else "partial", text,
                                                       self._params["language"], alternatives[0].get("confidence")))
                elif kind == "SpeechStarted":
                    await self._queue.put(STTEvent("speech_start"))
                elif kind == "Error":
                    await self._queue.put(STTEvent("error", msg.get("description", "error"), fatal=True))
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
        for task in self._tasks:
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
