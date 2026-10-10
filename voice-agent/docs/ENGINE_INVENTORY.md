# Engine inventory — what came from the bank voice agent

The source was the bank voice agent on the developer's Desktop (`BANK-VOICE-AGENT`), treated as
read-only. Every file in it is listed below with what happened to it.

Labels:

- **ENGINE** — copied unchanged, except the import line (`app.domain.*` became `app.lang.*`, because
  `app/domain/` is now the domain seam).
- **COUPLED** — engine code that named the bank or imported its prompts; copied, then the coupling was
  cut through `app/domain/base.py`.
- **DOMAIN** — banking behaviour; not copied.
- **NEVER COPY** — secrets, runtime data, recordings, generated files.

Engine behaviour is unchanged: no STT/TTS/VAD/barge-in, audio-path or provider setting was retuned,
and Sarvam remains the conversation LLM. The diffs below were checked with `diff`; "import only"
means the only changed line is the import.

## Application code

| Bank file | Label | Destination / note |
| --- | --- | --- |
| `app/__init__.py` | ENGINE | `app/__init__.py` |
| `app/audio/codecs.py` | ENGINE | `app/audio/codecs.py` — G.711 μ-law/A-law, resampling, output gain. Byte-identical. |
| `app/audio/vad.py` | ENGINE | `app/audio/vad.py` — energy VAD with adaptive floor. Byte-identical. |
| `app/config.py` | COUPLED | `app/config.py` — bank fields (`bank_name`, `customer_care_number`, `knowledge_dir`, `retrieval_top_k`, `full_context_max_chars`, single `default_language`) removed. Every engine setting kept with its original default. Added `default_inbound_language`/`default_outbound_language`, outbound pacing, CRM, RAG and real-estate settings. |
| `app/conversation/caller_audio.py` | ENGINE | Byte-identical. Audio → VAD → streaming STT → caller events. |
| `app/conversation/events.py` | ENGINE | Byte-identical. |
| `app/conversation/interruption.py` | ENGINE | Import only. Barge-in and backchannel detection. |
| `app/conversation/session.py` | COUPLED | `app/conversation/session.py` — the bank's "retrieve then answer" step and credential guard removed. Turn loop, barge-in, silence watchdog, sentence/clause streaming, filler timing and lifecycle kept. Added: the tool-calling loop (the bank had none), domain screening of caller turns and of model sentences, the domain's opening line, and a `CallRecord` handed to the domain at hangup. |
| `app/conversation/silence.py` | ENGINE | Byte-identical. |
| `app/conversation/speech.py` | ENGINE | Import only. Paced playback, streaming synthesis, stop on barge-in. |
| `app/conversation/turn_detection.py` | ENGINE | Import only. Dynamic end-of-turn detection. |
| `app/domain/devanagari.py` | COUPLED | `app/lang/devanagari.py` — banking words removed from the transliteration table (they are now left to the TTS). Everything else unchanged. |
| `app/domain/languages.py` | ENGINE (+gu) | `app/lang/languages.py` — Gujarati added (script ratio, a small word list, `gu-IN` STT tag). en/hi/mr scoring and the switch threshold are unchanged. |
| `app/domain/safety.py` | DOMAIN | Credential patterns (card/PIN/OTP/account numbers) were banking guardrails. The generic part, long-digit redaction, is `app/lang/redaction.py`; the domain supplies which words make short digit runs sensitive (`app/domain/real_estate/sensitive.py`). |
| `app/domain/spoken.py` | ENGINE (+gu) | `app/lang/spoken.py` — Gujarati entries added to the month/weekday/scale tables; Gujarati numbers stay as digits for the TTS. |
| `app/domain/text.py` | ENGINE (+gu) | `app/lang/text.py` — `gujarati_ratio()` added. |
| `app/llm/base.py` | ENGINE | Byte-identical. Provider-neutral messages, tool specs, tool calls, streaming events. |
| `app/llm/demo.py` | COUPLED | `app/llm/demo.py` — quoted the bank's knowledge block and helpline; now quotes the last tool result. Fake mode only. |
| `app/llm/fake.py` | ENGINE | Byte-identical. Scripted LLM for tests (`say`, `call`). |
| `app/llm/gemini.py` | ENGINE | Byte-identical. Optional fallback LLM. |
| `app/llm/router.py` | ENGINE | Byte-identical. Primary/fallback routing, first-token timeout, governor, leg samples. |
| `app/llm/sarvam.py` | ENGINE | Byte-identical. Sarvam chat completions with tool-call streaming. |
| `app/main.py` | COUPLED | `app/main.py` — `/ask` (bank knowledge probe) removed; `/healthz`, websocket, webhook and `/calls/recent` kept; `/metrics` added; the domain contributes its own routes (`/v1/calls/outbound`, `/v1/calls/details`). |
| `app/prompts/phrases.py` | DOMAIN | Bank greetings and helpline phrases. Replaced by `app/domain/real_estate/phrases.py`. |
| `app/prompts/system.py` | DOMAIN | Bank system prompt. Replaced by `app/domain/real_estate/prompt.py`. |
| `app/rag/ingestion.py` | DOMAIN | In-process knowledge loading for the bank's markdown. Knowledge now comes from the RAG service. |
| `app/rag/knowledge.py` | DOMAIN | Bank knowledge base and tenure tables. |
| `app/rag/metadata.py` | DOMAIN | |
| `app/rag/retrieval.py` | DOMAIN | In-memory lexical store. |
| `app/resilience/degradation.py` | ENGINE | Byte-identical. Leg monitors and new-call routing policy. |
| `app/resilience/probes.py` | COUPLED | `app/resilience/probes.py` — imported the system prompt and a hard-coded probe turn; the domain now supplies the probe messages. |
| `app/resilience/rate_governor.py` | ENGINE | Byte-identical. |
| `app/stt/base.py` | ENGINE | Byte-identical. |
| `app/stt/deepgram.py` | ENGINE | Byte-identical. |
| `app/stt/fake.py` | ENGINE | Byte-identical. |
| `app/stt/sarvam_streaming.py` | ENGINE (+gu) | Codemix streaming STT. `gu` added to the recognised language tags. |
| `app/telephony/base.py` | ENGINE | Byte-identical. |
| `app/telephony/voicelink.py` | ENGINE | Byte-identical. REST client (login, `add_lead`, routing) and media websocket transport. |
| `app/telephony/wire_format.py` | ENGINE | Byte-identical. |
| `app/tts/base.py` | ENGINE | Import only. Failover TTS. |
| `app/tts/cache.py` | COUPLED | `app/tts/cache.py` — imported the bank's phrase module; now takes the domain's `PhraseBook`. Preload/warm behaviour unchanged. |
| `app/tts/fake.py` | ENGINE | Import only. |
| `app/tts/sarvam_streaming.py` | ENGINE (+gu) | Import plus `gu: gu-IN` in the language codes. Speaker (`ishita`), pace, gain and buffering unchanged. |
| `app/wiring.py` | COUPLED | `app/wiring.py` — no longer loads a knowledge base; builds the engine, then hands `EngineServices` to the domain plugin. |
| all `__init__.py` | ENGINE | |

## Not present in the bank agent (built new in `/voice-agent`)

The bank agent answered inbound questions only. These engine pieces did not exist there and were
written for this agent; they contain no domain logic.

| New file | Why |
| --- | --- |
| `app/domain/base.py` | The seam: `DomainPlugin`, `Conversation`, `PhraseBook`, `Tool`, `ToolOutcome`, `CallerTurnAction`, `CallRecord`. |
| `app/outbound/registry.py` | SQLite registry tying an outbound request to the call that connects; local do-not-call suppression cache. |
| `app/outbound/dialer.py` | Paced outbound queue in front of VoiceLink `add_lead`: suppression, domain dial policy, in-flight cap, pause on STT degradation, no-answer expiry. |
| `app/outbox/outbox.py` | Durable SQLite retry outbox with exponential backoff and idempotency keys. |
| `app/observability/logging.py`, `metrics.py` | JSON logs, in-process counters. |
| `app/lang/redaction.py` | Domain-neutral digit redaction (from the generic half of the bank's `safety.py`). |

## Everything else

| Bank file | Label | Note |
| --- | --- | --- |
| `.env` | NEVER COPY | Live credentials. Not read beyond key names. |
| `.env.example` | COUPLED | Rewritten as `voice-agent/.env.example`; every value blank or `REPLACE_…`; bank keys removed. |
| `.dockerignore`, `.gitignore` | ENGINE | Copied and adjusted (no `data/` in the image). |
| `Dockerfile` | COUPLED | Copied; no longer copies `data/` (the bank's knowledge documents). |
| `requirements.txt` | COUPLED | `pypdf` (bank knowledge loading) removed; `tzdata` added for Asia/Kolkata. |
| `README.md` | DOMAIN | Bank product description. Replaced by `voice-agent/README.md`. |
| `data/knowledge/sunrise_bank.md` | DOMAIN | Bank documentation. |
| `data/runtime/phrases/**` (30 `.ulaw` clips) | NEVER COPY | Pre-rendered bank greetings. The real-estate phrases render at startup. |
| `deploy/azure/azure.env` | NEVER COPY | Live credentials. |
| `deploy/azure/deploy.log` | NEVER COPY | Runtime log. |
| `deploy/azure/.gitignore`, `azure.env.example`, `deploy.sh`, `render_app.py` | NOT COPIED | estate-os already has `deploy/azure/` for the whole stack; it now points at `/voice-agent`. |
| `deploy/azure/routing.py`, `voicelink_route.py` | NOT COPIED | One-off DID routing helpers for the bank's shared number. |
| `evals/answers.py` | DOMAIN | Bank answer evals. |
| `tests/test_knowledge.py` | DOMAIN | Bank retrieval facts. |
| `tests/test_service.py` | COUPLED | Health and websocket tests ported to `tests/test_service.py`; credential-guard tests were banking guardrails and were dropped. |
| `tests/test_session.py` | COUPLED | Timing, sentence-split and silence tests ported to `tests/test_session.py` with a stub domain; the retrieval assertions were dropped. |
| `tests/test_speech_text.py` | COUPLED | Ported with property figures instead of bank figures. |
| `voice-samples/*.wav` | NEVER COPY | Voice audition recordings. |
| `.venv/`, `.pytest_cache/`, `__pycache__/` | NEVER COPY | |

## Engine tests

`tests/test_session.py`, `test_tool_loop.py`, `test_speech_text.py`, `test_languages.py`,
`test_audio_pipeline.py`, `test_resilience.py`, `test_outbound_and_outbox.py`,
`test_phrase_cache.py`, `test_service.py` run with `PROVIDER_MODE=fake` and no network.
`test_no_banking_vocabulary.py` fails if banking vocabulary appears anywhere under `app/`, with one
exception: `app/domain/real_estate/sensitive.py`, which must name identity documents and financing
promises in order to refuse them.
