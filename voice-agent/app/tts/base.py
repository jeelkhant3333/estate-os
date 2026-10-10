"""TTS interface and controlled primary → fallback switch (spec sections 6, 30)."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Protocol

from app.lang.languages import Lang

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, language: Lang) -> AsyncIterator[bytes]:
        """Yield raw 8 kHz μ-law audio."""
        ...

    async def cancel(self) -> None: ...
    async def close(self) -> None: ...


async def _close(agen) -> None:
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass


class FailoverTTS:
    def __init__(self, primary: TTSProvider, fallback: TTSProvider, first_audio_timeout_s: float = 2.0,
                 on_fallback: Callable[[str], None] | None = None):
        self.name = "failover_tts"
        self.primary = primary
        self.fallback = fallback
        self.first_audio_timeout_s = first_audio_timeout_s
        self.on_fallback = on_fallback
        self.use_fallback = False
        self._active: TTSProvider | None = None

    def _switch(self, reason: str) -> None:
        if not self.use_fallback:
            log.warning("TTS primary failed (%s); using fallback for the rest of the call", reason)
            self.use_fallback = True
            if self.on_fallback:
                self.on_fallback(reason)

    async def synthesize(self, text: str, language: Lang) -> AsyncIterator[bytes]:
        if not self.use_fallback:
            self._active = self.primary
            agen = self.primary.synthesize(text, language)
            try:
                first = await asyncio.wait_for(agen.__anext__(), self.first_audio_timeout_s)
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, TTSError, OSError, Exception) as exc:  # noqa: BLE001 - any provider failure
                await _close(agen)
                self._switch(repr(exc))
            else:
                yield first
                try:
                    async for chunk in agen:
                        yield chunk
                    return
                except Exception as exc:  # noqa: BLE001
                    self._switch(repr(exc))
                    raise TTSError("primary failed mid-utterance") from exc
        self._active = self.fallback
        try:
            async for chunk in self.fallback.synthesize(text, language):
                yield chunk
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"fallback TTS failed: {exc!r}") from exc

    async def cancel(self) -> None:
        if self._active is not None:
            try:
                await self._active.cancel()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        for provider in (self.primary, self.fallback):
            try:
                await provider.close()
            except Exception:  # noqa: BLE001
                pass
