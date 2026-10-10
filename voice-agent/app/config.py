"""Runtime configuration: the voice engine first, then the real-estate domain and its services."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    app_env: str = "dev"
    provider_mode: Literal["fake", "live"] = "fake"
    log_level: str = "INFO"
    # text locally; json in production so log lines carry call_id and prompt_version as fields.
    log_format: Literal["text", "json"] = "text"

    timezone: str = "Asia/Kolkata"
    # Language a caller is greeted in when the lead's own language is not known yet.
    # Riya speaks Hindi, Marathi and English only.
    default_inbound_language: Literal["mr", "hi", "en"] = "hi"
    default_outbound_language: Literal["mr", "hi", "en"] = "mr"
    # How sure the detector must be to switch mid-call. Hindi and Marathi overlap heavily, so a
    # real switch often scores just above chance; the STT's own tag can override this.
    language_switch_confidence: float = 0.6

    runtime_dir: Path = REPO_ROOT / "data" / "runtime"
    # SQLite journal for the stores in runtime_dir. WAL needs shared memory, which a network file
    # share (Azure Files) cannot provide across hosts: during a restart the new replica could not
    # open a file the old one still held in WAL mode, so the restart never completed. Use DELETE
    # when runtime_dir is a network mount.
    sqlite_journal_mode: Literal["WAL", "DELETE"] = "WAL"
    # Scratch space for per-call debug audio. Never put this on network storage: it writes a file
    # per call. Defaults to runtime_dir when unset.
    cache_dir: Path | None = None
    # Where pre-rendered phrase audio lives. This one SHOULD be durable, so a restart does not
    # force every phrase to be re-synthesised while the first caller waits. It is fully loaded
    # into memory at startup, so its location never costs anything during a call.
    phrase_cache_dir: Path | None = None

    # Telephony (VoiceLink REST confirmed from its OpenAPI spec; media websocket protocol still assumed)
    voice_link_base_url: str = "https://app.voicelink.co.in/api"
    voice_link_username: str = ""
    voice_link_password: str = ""
    voice_link_did_number: str = ""
    voice_link_client_id: int | None = None
    voice_link_webhook_url: str = ""
    voice_link_webhook_token: str = ""
    voice_link_capture_messages: int = 20
    voice_link_default_audio_format: str = "ulaw"
    voice_link_out_chunk_ms: int = 40
    voice_link_debug_audio: bool = False
    public_ws_base_url: str = ""
    max_concurrent_calls: int = 20

    # Sarvam
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    # "streaming" = speech-to-text/ws with client flush (CL-006); "realtime" = the older speech-to-text-realtime socket
    sarvam_stt_api: Literal["streaming", "realtime"] = "streaming"
    sarvam_stt_streaming_ws_url: str = "wss://api.sarvam.ai/speech-to-text/ws"
    sarvam_stt_streaming_model: str = "saaras:v3"
    sarvam_stt_streaming_language_code: str = "unknown"
    sarvam_stt_high_vad_sensitivity: bool = True
    stt_flush_after_ms: int = 150
    sarvam_stt_ws_url: str = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
    sarvam_stt_model: str = "saaras:v3-realtime"
    sarvam_stt_stream_type: str = "fast"
    sarvam_stt_mode: str = "codemix"
    sarvam_stt_language_code: str = "auto"
    sarvam_tts_speaker: str = "ishita"
    sarvam_tts_ws_url: str = "wss://api.sarvam.ai/text-to-speech/ws"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_pace: float = 1.0
    sarvam_tts_preprocessing: bool = True

    # Primary TTS: sarvam_streaming (Bulbul v3) or gnani (Vachana, Bulbul behind it)
    tts_primary: Literal["sarvam_streaming", "gnani"] = "sarvam_streaming"

    # Deepgram
    # Primary STT: sarvam (Saaras v3 streaming, Deepgram as fallback) or deepgram (Nova-3, Sarvam as fallback).
    stt_primary: Literal["sarvam", "deepgram", "gnani"] = "sarvam"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"

    # Gnani Vachana (experiment): STT_PRIMARY=gnani, TTS_PRIMARY=gnani and/or LLM_PROVIDER=gnani.
    # Sarvam stays behind each of them as the fallback.
    gnani_api_key: str = ""
    gnani_stt_url: str = "wss://api.vachana.ai/stt/v3/stream"
    gnani_stt_min_silence_ms: int = 500
    gnani_stt_vad_threshold: float = 0.7
    gnani_tts_url: str = "wss://api.vachana.ai/api/v1/tts"
    gnani_tts_model: str = "timbre-v2.5"
    gnani_tts_voice_hi: str = "Nalini"
    gnani_tts_voice_mr: str = "Zahira"
    gnani_tts_voice_en: str = "Kaveri"
    gnani_tts_speed: float = 1.0
    # The conversation model: sarvam (default) or gnani (Evon, an OpenAI-compatible API).
    llm_provider: Literal["sarvam", "gnani"] = "sarvam"
    gnani_llm_base_url: str = ""
    gnani_llm_model: str = ""
    gnani_llm_api_key: str = ""  # empty: GNANI_API_KEY
    gnani_llm_auth_header: str = "Authorization"

    # Linear gain applied to outgoing telephony audio. 1.0 leaves the TTS level untouched.
    tts_output_gain: float = 1.0

    # LLM
    llm_primary_model: str = "sarvam-105b-conversations"
    llm_primary_reasoning_effort: str = "none"
    llm_fallback_model: str = "gemini-3.5-flash-lite"
    google_cloud_project: str = ""
    google_cloud_location: str = "asia-south1"
    llm_max_tokens: int = 200
    llm_temperature: float = 0.2

    # Rate governor (spec section 7)
    sarvam_llm_rate_limit_per_min: int = 120
    governor_safety_fraction: float = 0.85
    governor_live_max_wait_ms: int = 150

    # Hard-failure and degradation rules (spec section 30)
    first_token_timeout_ms: int = 2000
    llm_alert_p95_ms: float = 750
    llm_trip_p95_ms: float = 1000
    llm_recover_p95_ms: float = 700
    stt_alert_p95_ms: float = 600
    stt_trip_p95_ms: float = 800
    stt_recover_p95_ms: float = 500
    stt_trip_error_rate: float = 0.02
    stt_recover_error_rate: float = 0.005
    degradation_trip_minutes: int = 3
    degradation_recover_minutes: int = 10
    degradation_window_s: int = 300
    degradation_min_samples: int = 50

    # Conversation timing
    silence_first_prompt_s: float = 6.0
    silence_prompt_interval_s: float = 5.0
    # The "one moment" filler plays only when the answer's first sentence is this late (0 = never).
    filler_after_ms: int = 1200
    barge_in_min_speech_ms: int = 300
    # CL-008: rule-based router answers simple price/availability/possession/amenity/project questions with one LLM call
    # 0 = routed lookups are acknowledged immediately; N = only when the answer has not started within N ms
    lookup_ack_after_ms: int = 0
    # CL-010 Devanagari-only speech, switched off by CL-014: "en" = follow the caller, English words allowed
    english_reply_language: Literal["hi", "mr", "en"] = "en"
    devanagari_only_speech: bool = False
    # CL-011: merge a turn into the caller's continued speech if they resume within this long, before Riya speaks (0 = off)
    turn_resume_merge_ms: int = 200

    # Outbound pacing and admin access. ADMIN_API_TOKEN is the CRM's VOICE_AGENT_API_KEY.
    admin_api_token: str = ""
    llm_probe_interval_s: float = 30.0
    # ---- per-call cost log (telephony excluded); defaults are Sarvam's published prices in INR
    cost_stt_per_hour: float = 30.0
    cost_tts_per_1k_chars: float = 3.0
    cost_llm_input_per_m: float = 29.28
    cost_llm_cached_per_m: float = 10.98
    cost_llm_output_per_m: float = 73.20
    cost_currency: str = "INR"
    outbound_max_in_flight: int = 5
    outbound_min_interval_s: float = 2.0
    outbound_dial_timeout_s: float = 180.0
    nova3_floor_passed: bool = False
    # Model calls per caller turn (tool hops plus the answer) and turns of history replayed.
    max_tool_hops: int = 3
    history_turns: int = 4

    # ---- Real-estate domain
    workspace_id: str = "ws_demo"
    # The company Riya speaks for. When set it always wins; when blank, the CRM workspace's name is
    # used (e.g. "Westhaven Realty · Demo" -> "Westhaven Realty").
    builder_name: str = ""
    # Spoken once in the opening line.
    disclose_ai: bool = True
    disclose_recording: bool = True
    # TRAI calling window for outbound calls, local time (Asia/Kolkata).
    calling_hours_start: str = "09:00"
    calling_hours_end: str = "21:00"
    # Comma-separated test numbers (your own phones) that may be called outside the window above,
    # for testing at any hour. Matched on the last 10 digits. The do-not-call check still applies.
    # Never put a customer's number here.
    test_phone_allowlist: str = ""
    # A sales line for warm transfer. Blank: escalation becomes a scheduled callback + handover.
    sales_transfer_number: str = ""
    # Whether the provider supports a live transfer at all (VoiceLink: not documented yet).
    live_transfer_enabled: bool = False

    # ---- CRM (system of record)
    crm_mode: Literal["mock", "http"] = "mock"
    crm_base_url: str = "http://localhost:8080"
    crm_workspace_id: int = 1
    crm_service_email: str = ""
    crm_service_password: str = ""
    crm_token_refresh_margin_s: int = 120
    crm_catalog_refresh_s: int = 900
    crm_timeout_s: float = 2.5

    # ---- RAG (knowledge service)
    rag_service_url: str = ""
    rag_voice_token: str = ""
    rag_timeout_ms: int = 600
    rag_top_k: int = 4


    @field_validator("voice_link_client_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object, info) -> object:
        """A commented-out or emptied line in .env means "use the default", not a parse error.

        Without this, `VOICE_LINK_DEBUG_AUDIO=` fails startup with a bool-parsing error that says
        nothing about which file the caller needs to edit.
        """
        if isinstance(value, str) and not value.strip():
            field = cls.model_fields.get(info.field_name)
            if field is not None and field.default is not None and not isinstance(field.default, str):
                return field.default
        return value

    @property
    def audio_cache_dir(self) -> Path:
        return self.cache_dir or self.runtime_dir

    @property
    def phrases_dir(self) -> Path:
        return self.phrase_cache_dir or self.runtime_dir

    @property
    def llm_fallback_enabled(self) -> bool:
        """LLM_FALLBACK_MODEL=none runs Sarvam without a fallback LLM."""
        return self.llm_fallback_model.strip().lower() not in ("", "none", "off", "disabled")

    @property
    def reasoning_effort(self) -> str | None:
        value = self.llm_primary_reasoning_effort.strip().lower()
        return None if value in ("", "none", "null", "off") else value

    @property
    def governor_limit_per_min(self) -> int:
        return max(1, int(self.sarvam_llm_rate_limit_per_min * self.governor_safety_fraction))


def cost_prices(s: "Settings"):
    from app.observability.cost import Prices
    return Prices(s.cost_stt_per_hour, s.cost_tts_per_1k_chars, s.cost_llm_input_per_m,
                  s.cost_llm_cached_per_m, s.cost_llm_output_per_m, s.cost_currency)


@lru_cache
def get_settings() -> Settings:
    return Settings()