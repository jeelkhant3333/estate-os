"""Builds everything a call needs, once, at startup."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings
from app.conversation.session import CallSession, SessionDeps
from app.conversation.turn_detection import TurnConfig
from app.domain.base import DomainPlugin, PhraseBook
from app.lang.devanagari import to_devanagari_speech
from app.llm.base import LLMProvider
from app.llm.demo import DemoLLM
from app.llm.router import LLMRouter
from app.observability.metrics import Metrics
from app.outbound.dialer import OutboundDialer
from app.outbound.registry import CallRegistry
from app.outbox.outbox import Outbox
from app.resilience.degradation import LegMonitor, LegThresholds, RoutingPolicy
from app.resilience.rate_governor import RateGovernor
from app.stt.fake import SilentSTT
from app.telephony.voicelink import VoiceLinkClient
from app.tts.base import FailoverTTS
from app.tts.cache import PhraseAudioCache
from app.tts.fake import SilenceTTS

log = logging.getLogger(__name__)


@dataclass
class EngineServices:
    """Engine facilities a domain plugin may use. Nothing here knows what the calls are about."""

    settings: Settings
    router: LLMRouter
    governor: RateGovernor
    registry: CallRegistry
    dialer: OutboundDialer
    outbox: Outbox
    metrics: Metrics
    active_calls: Callable[[], int]


PluginFactory = Callable[[EngineServices], DomainPlugin]


@dataclass
class Container:
    settings: Settings
    plugin: DomainPlugin
    router: LLMRouter
    governor: RateGovernor
    llm_leg: LegMonitor
    stt_leg: LegMonitor
    routing: RoutingPolicy
    phrase_cache: PhraseAudioCache
    voicelink: VoiceLinkClient | None
    registry: CallRegistry
    dialer: OutboundDialer
    outbox: Outbox
    metrics: Metrics
    active_calls: dict[str, CallSession] = field(default_factory=dict)
    recent_metrics: deque = field(default_factory=lambda: deque(maxlen=200))
    recent_webhooks: deque = field(default_factory=lambda: deque(maxlen=200))
    background: set = field(default_factory=set)

    def session_deps(self) -> SessionDeps:
        s = self.settings
        return SessionDeps(
            router=self.router,
            phrases=self.plugin.phrases,
            default_language=s.default_inbound_language,
            language_switch_confidence=s.language_switch_confidence,
            llm_max_tokens=s.llm_max_tokens,
            llm_temperature=s.llm_temperature,
            devanagari_speech=_devanagari_speech(s),
            turn_config=TurnConfig(),
            silence_first_prompt_s=s.silence_first_prompt_s,
            silence_interval_s=s.silence_prompt_interval_s,
            barge_in_min_speech_ms=s.barge_in_min_speech_ms,
            filler_after_ms=s.filler_after_ms,
            max_tool_hops=s.max_tool_hops,
            history_turns=s.history_turns,
            sensitive=self.plugin.is_sensitive,
        )

    def stt_factories(self, language: str):
        s = self.settings
        if s.provider_mode == "fake":
            return (lambda: SilentSTT()), (lambda: SilentSTT())
        from app.stt.deepgram import DeepgramSTT
        from app.stt.sarvam_streaming import SarvamStreamingSTT

        def primary():
            return SarvamStreamingSTT(
                s.sarvam_api_key, s.sarvam_stt_streaming_ws_url, s.sarvam_stt_streaming_model,
                language_code=s.sarvam_stt_streaming_language_code, mode=s.sarvam_stt_mode,
                high_vad_sensitivity=s.sarvam_stt_high_vad_sensitivity)

        def deepgram():
            # Nova-3 has no Hindi-only streaming model worth using on calls; its multilingual mode
            # handles Hindi with English mixed in. Marathi and English are monolingual.
            code = {"hi": "multi", "mr": "mr", "en": "en"}.get(language, "multi")
            return DeepgramSTT(s.deepgram_api_key, s.deepgram_model, language=code)

        def gnani():
            from app.stt.gnani import GnaniSTT
            code = {"hi": "hi-IN", "mr": "mr-IN", "en": "en-IN"}.get(language, "hi-IN")
            return GnaniSTT(s.gnani_api_key, code, s.gnani_stt_url, s.gnani_stt_min_silence_ms,
                            s.gnani_stt_vad_threshold)

        if s.stt_primary == "gnani" and s.gnani_api_key:
            return gnani, primary
        if s.stt_primary == "deepgram" and s.deepgram_api_key:
            return deepgram, primary
        return primary, deepgram

    def new_tts(self) -> FailoverTTS:
        s = self.settings
        if s.provider_mode == "fake":
            return FailoverTTS(SilenceTTS("silence_primary"), SilenceTTS("silence_fallback"))
        from app.tts.sarvam_streaming import SarvamStreamingTTS

        def bulbul_tts() -> SarvamStreamingTTS:
            return SarvamStreamingTTS(s.sarvam_api_key, s.sarvam_tts_ws_url, s.sarvam_tts_model,
                                      s.sarvam_tts_speaker, s.sarvam_tts_pace,
                                      s.sarvam_tts_preprocessing)

        bulbul = bulbul_tts()
        if s.tts_primary == "gnani" and s.gnani_api_key:
            from app.tts.gnani import GnaniTTS
            gnani = GnaniTTS(s.gnani_api_key, {"hi": s.gnani_tts_voice_hi, "mr": s.gnani_tts_voice_mr,
                                               "en": s.gnani_tts_voice_en}, s.gnani_tts_model, s.gnani_tts_speed,
                             s.gnani_tts_url)
            return FailoverTTS(gnani, bulbul)
        # The fallback is Bulbul again on its own socket: a stalled connection is replaced, and the
        # caller keeps hearing the same voice for the whole call.
        return FailoverTTS(bulbul, bulbul_tts())

    def track(self, task) -> None:
        """Keep a reference to fire-and-forget work so it is not garbage-collected mid-flight."""
        self.background.add(task)
        task.add_done_callback(self.background.discard)


def _devanagari_speech(s: Settings) -> bool:
    return s.devanagari_only_speech or s.default_inbound_language in ("hi", "mr")


def _speech_text(s: Settings, phrases: PhraseBook):
    """The text actually synthesised for a phrase, matching what a call speaks."""
    if not _devanagari_speech(s):
        return lambda text, _lang: text
    return lambda text, lang: to_devanagari_speech(text, lang, phrases.names)


def _require(settings: Settings) -> None:
    missing = [n for n in ("sarvam_api_key",) if not getattr(settings, n)]
    if missing:
        raise RuntimeError(
            f"PROVIDER_MODE=live needs: {', '.join(m.upper() for m in missing)}")


def _default_plugin(services: EngineServices) -> DomainPlugin:
    from app.domain.real_estate.plugin import RealEstatePlugin

    return RealEstatePlugin(services)


def domain_routes(container_dep, admin_dep):
    """HTTP routes the domain adds to the service (the composition root is the only place that
    names the domain; the engine modules never import it)."""
    from app.domain.real_estate.api import build_router

    return build_router(container_dep, admin_dep)


def build_container(settings: Settings, plugin_factory: PluginFactory | None = None,
                    llm: LLMProvider | None = None) -> Container:
    """`llm` replaces the conversation model (tests and the scripted end-to-end smoke)."""
    governor = RateGovernor(settings.governor_limit_per_min)
    common = dict(trip_minutes=settings.degradation_trip_minutes,
                  recover_minutes=settings.degradation_recover_minutes,
                  window_s=settings.degradation_window_s,
                  min_samples=settings.degradation_min_samples)
    llm_leg = LegMonitor("llm", LegThresholds(trip_p95_ms=settings.llm_trip_p95_ms,
                                              recover_p95_ms=settings.llm_recover_p95_ms,
                                              alert_p95_ms=settings.llm_alert_p95_ms, **common))
    stt_leg = LegMonitor("stt", LegThresholds(trip_p95_ms=settings.stt_trip_p95_ms,
                                              recover_p95_ms=settings.stt_recover_p95_ms,
                                              alert_p95_ms=settings.stt_alert_p95_ms,
                                              trip_error_rate=settings.stt_trip_error_rate,
                                              recover_error_rate=settings.stt_recover_error_rate,
                                              **common))
    routing = RoutingPolicy(llm_leg, stt_leg, settings.nova3_floor_passed,
                            llm_fallback_available=settings.llm_fallback_enabled)

    if settings.provider_mode == "live":
        _require(settings)
        from app.llm.sarvam import SarvamLLM

        primary: Any = SarvamLLM(settings.sarvam_api_key, settings.llm_primary_model,
                                 settings.sarvam_base_url, settings.reasoning_effort)
        fallback: Any = None
        if settings.llm_provider == "gnani" and settings.gnani_llm_base_url and settings.gnani_llm_model:
            from app.llm.openai_compat import OpenAICompatLLM

            # Gnani leads; Sarvam answers when it fails or is slow.
            primary, fallback = OpenAICompatLLM(
                "gnani", settings.gnani_llm_api_key or settings.gnani_api_key, settings.gnani_llm_model,
                settings.gnani_llm_base_url, settings.gnani_llm_auth_header), primary
        if fallback is None and settings.llm_fallback_enabled and settings.google_cloud_project:
            from app.llm.gemini import GeminiLLM

            fallback = GeminiLLM(settings.llm_fallback_model, settings.google_cloud_project,
                                 settings.google_cloud_location)
        voicelink = (VoiceLinkClient(settings.voice_link_base_url, settings.voice_link_username,
                                     settings.voice_link_password)
                     if settings.voice_link_username else None)
        voice_id = (f"gnani_{settings.gnani_tts_model}_{settings.gnani_tts_voice_hi}"
                    if settings.tts_primary == "gnani" and settings.gnani_api_key
                    else f"sarvam_{settings.sarvam_tts_model.replace(':', '')}_{settings.sarvam_tts_speaker}")
    else:
        primary, fallback, voicelink, voice_id = DemoLLM("sarvam"), None, None, "fake"
    if llm is not None:
        primary, fallback = llm, None

    router = LLMRouter(primary, fallback, governor=governor, leg=llm_leg,
                       first_token_timeout_s=settings.first_token_timeout_ms / 1000,
                       live_max_wait_s=settings.governor_live_max_wait_ms / 1000)
    registry = CallRegistry(settings.runtime_dir / "calls.sqlite", journal_mode=settings.sqlite_journal_mode)
    outbox = Outbox(settings.runtime_dir / "outbox.sqlite", journal_mode=settings.sqlite_journal_mode)
    metrics = Metrics()
    ws_base = settings.public_ws_base_url.rstrip("/")
    dialer = OutboundDialer(
        registry, voicelink, did_number=settings.voice_link_did_number,
        websocket_url=f"{ws_base}/telephony/voicelink/ws" if ws_base else None,
        webhook_url=settings.voice_link_webhook_url or None,
        paused=lambda: routing.outbound_paused,
        max_in_flight=settings.outbound_max_in_flight,
        min_interval_s=settings.outbound_min_interval_s,
        dial_timeout_s=settings.outbound_dial_timeout_s)

    active: dict[str, CallSession] = {}
    services = EngineServices(settings=settings, router=router, governor=governor,
                              registry=registry, dialer=dialer, outbox=outbox, metrics=metrics,
                              active_calls=lambda: len(active))
    plugin = (plugin_factory or _default_plugin)(services)
    container = Container(
        settings=settings, plugin=plugin, router=router, governor=governor,
        llm_leg=llm_leg, stt_leg=stt_leg, routing=routing,
        phrase_cache=PhraseAudioCache(settings.phrases_dir / "phrases", voice_id, plugin.phrases,
                                      speak=_speech_text(settings, plugin.phrases)),
        voicelink=voicelink, registry=registry, dialer=dialer, outbox=outbox, metrics=metrics,
        active_calls=active,
    )
    return container
