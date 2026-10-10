"""Renders the voice agent's Container App definition.

The agent needs a YAML definition rather than plain `az containerapp` flags because it mounts an
Azure Files volume: its do-not-call suppression list and outbound-call registry are SQLite files
that must outlive a replica.
"""

from __future__ import annotations

import json
import os
import sys

# Secrets are referenced from the container as secretref:<name>; their values live on the app.
SECRETS = {
    "acr-password": "ACR_PASS",
    "crm-service-password": "CRM_SERVICE_PASSWORD",
    "admin-token": "VOICE_AGENT_API_KEY",
    "sarvam-key": "SARVAM_API_KEY",
    "deepgram-key": "DEEPGRAM_API_KEY",
    "gnani-key": "GNANI_API_KEY",
    "voicelink-password": "VOICE_LINK_PASSWORD",
    "voicelink-webhook-token": "VOICE_LINK_WEBHOOK_TOKEN",
    "rag-voice-token": "RAG_VOICE_TOKEN",
}

# Environment variable -> secret name, wired only when that secret has a value.
SECRET_ENV = {
    "ADMIN_API_TOKEN": "admin-token",
    "CRM_SERVICE_PASSWORD": "crm-service-password",
    "SARVAM_API_KEY": "sarvam-key",
    "DEEPGRAM_API_KEY": "deepgram-key",
    "GNANI_API_KEY": "gnani-key",
    "VOICE_LINK_PASSWORD": "voicelink-password",
    "VOICE_LINK_WEBHOOK_TOKEN": "voicelink-webhook-token",
    "RAG_VOICE_TOKEN": "rag-voice-token",
}

# Plain settings, passed through from azure.env with a default when unset.
PLAIN = {
    "APP_ENV": "production",
    "LOG_LEVEL": "INFO",
    "PROVIDER_MODE": "fake",
    "WORKSPACE_ID": "ws_demo",
    "BUILDER_NAME": "XYZ Realty",
    "DEFAULT_OUTBOUND_LANGUAGE": "mr",
    # Your own phones, callable outside 09:00-21:00 IST for testing. Never a customer's number.
    "TEST_PHONE_ALLOWLIST": "",
    "CRM_MODE": "http",
    "CRM_BASE_URL": "https://placeholder.invalid",
    "CRM_WORKSPACE_ID": "1",
    "CRM_SERVICE_EMAIL": "",
    "CRM_TOKEN_REFRESH_MARGIN_S": "120",
    "CRM_CATALOG_REFRESH_S": "900",
    # The knowledge service (brochures, FAQs, charges) over the environment's internal network.
    "RAG_SERVICE_URL": "",
    "LANGUAGE_SWITCH_CONFIDENCE": "0.6",
    "VOICE_LINK_BASE_URL": "https://app.voicelink.co.in/api",
    "VOICE_LINK_USERNAME": "",
    "VOICE_LINK_DID_NUMBER": "",
    "VOICE_LINK_CLIENT_ID": "",
    "VOICE_LINK_WEBHOOK_URL": "",
    "PUBLIC_WS_BASE_URL": "",
    "LLM_FALLBACK_MODEL": "none",
    # Speech and model tuning. These differ from the code defaults in a tuned deployment, so they
    # are passed explicitly rather than left to fall back.
    "LLM_PRIMARY_MODEL": "sarvam-105b-conversations",
    "LLM_PRIMARY_REASONING_EFFORT": "none",
    "SARVAM_LLM_RATE_LIMIT_PER_MIN": "120",
    "SARVAM_STT_API": "streaming",
    "SARVAM_TTS_SPEAKER": "ishita",
    "NOVA3_FLOOR_PASSED": "false",
    "VOICE_LINK_CAPTURE_MESSAGES": "20",
    # Which TTS engine actually speaks. The code default is sarvam_streaming, so leaving this
    # unset silently swaps the voice for a different engine than the one tuned locally.
    "TTS_PRIMARY": "sarvam_streaming",
    # Which STT listens: sarvam (default) or deepgram.
    "STT_PRIMARY": "sarvam",
    # Gnani experiment (STT_PRIMARY=gnani, TTS_PRIMARY=gnani, LLM_PROVIDER=gnani); Sarvam stays fallback.
    "LLM_PROVIDER": "sarvam",
    "GNANI_TTS_VOICE_HI": "Nalini",
    "GNANI_TTS_VOICE_MR": "Zahira",
    "GNANI_TTS_VOICE_EN": "Kaveri",
    "GNANI_TTS_SPEED": "1.0",
    "GNANI_STT_MIN_SILENCE_MS": "500",
    "GNANI_LLM_BASE_URL": "",
    "GNANI_LLM_MODEL": "",
    "GNANI_LLM_AUTH_HEADER": "Authorization",
    "SARVAM_TTS_MODEL": "bulbul:v3",
    "SARVAM_TTS_PACE": "1.0",
    # Speech recognition tuning.
    "SARVAM_STT_MODEL": "saaras:v3-realtime",
    "SARVAM_STT_STREAMING_MODEL": "saaras:v3",
    "SARVAM_STT_STREAM_TYPE": "fast",
    "SARVAM_STT_MODE": "codemix",
    "SARVAM_STT_LANGUAGE_CODE": "auto",
    "SARVAM_STT_STREAMING_LANGUAGE_CODE": "unknown",
    # High sensitivity turned background noise on phone lines into "speech" (and interruptions).
    "SARVAM_STT_HIGH_VAD_SENSITIVITY": "false",
    # Speech without recognisable words must last this long to interrupt Riya (noise is shorter).
    "BARGE_IN_MIN_SPEECH_MS": "600",
    "STT_FLUSH_AFTER_MS": "150",
    "GOOGLE_CLOUD_PROJECT": "",
    "GOOGLE_CLOUD_LOCATION": "asia-south1",
    # data/runtime is the Azure Files mount; only durable state belongs here.
    "RUNTIME_DIR": "/srv/data/runtime",
    # WAL needs shared memory that an Azure Files mount cannot share between the old and the new
    # replica during a restart, which then never completes. The rollback journal works.
    "SQLITE_JOURNAL_MODE": "DELETE",
    # Regenerable audio caches go on the container's local disk. On the network mount the
    # synchronous phrase-cache read on the speech path stalls the event loop and chops the audio.
    # Per-call debug audio goes to the container's local disk: it writes a file per call, which
    # must never touch the network mount.
    "CACHE_DIR": "/srv/cache",
    # Pre-rendered phrase audio goes on the durable mount so a restart does not re-synthesise
    # every greeting while the first caller waits. It is loaded into memory at startup.
    "PHRASE_CACHE_DIR": "/srv/data/runtime",
    # Captures what the agent actually emits, for diagnosing audio quality. Off by default.
    "VOICE_LINK_DEBUG_AUDIO": "false",
    # A Hindi or Marathi caller should not be greeted in English and have to overcome the
    # language-switch threshold before being understood.
    "DEFAULT_INBOUND_LANGUAGE": "hi",
    "TTS_OUTPUT_GAIN": "1.0",
}


def main() -> None:
    required = ["ACR_SERVER", "ACR_USER", "ACR_PASS", "TAG", "ENV_ID", "LOCATION"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        sys.exit(f"render_agent.py needs these environment variables: {', '.join(missing)}")

    # Container Apps rejects a secret declared with an empty value, and in fake mode the provider
    # keys are deliberately unset. Declare only the secrets that actually have a value, and omit
    # the matching environment variables; the agent's settings already default them to empty.
    present = {name: os.environ.get(source, "") for name, source in SECRETS.items()}
    present = {name: value for name, value in present.items() if value}
    if "acr-password" not in present:
        sys.exit("ACR_PASS must be set to pull the image")

    env = [{"name": key, "value": os.environ.get(key, default)} for key, default in PLAIN.items()]
    env += [{"name": key, "secretRef": secret}
            for key, secret in SECRET_ENV.items() if secret in present]

    app = {
        "location": os.environ["LOCATION"],
        "properties": {
            "managedEnvironmentId": os.environ["ENV_ID"],
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "external": True,
                    "targetPort": 8080,
                    # auto keeps HTTP/1.1, which the VoiceLink media websocket upgrade needs.
                    "transport": "auto",
                    "allowInsecure": False,
                    "stickySessions": {"affinity": "sticky"},
                },
                "registries": [{
                    "server": os.environ["ACR_SERVER"],
                    "username": os.environ["ACR_USER"],
                    "passwordSecretRef": "acr-password",
                }],
                "secrets": [{"name": name, "value": value} for name, value in present.items()],
            },
            "template": {
                "containers": [{
                    "name": "voice-agent",
                    "image": f"{os.environ['ACR_SERVER']}/voice-agent:{os.environ['TAG']}",
                    "env": env,
                    "resources": {"cpu": 1.0, "memory": "2Gi"},
                    "volumeMounts": [{"volumeName": "state", "mountPath": "/srv/data/runtime"}],
                    "probes": [{
                        "type": "Liveness",
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "initialDelaySeconds": 20,
                        "periodSeconds": 30,
                    }],
                }],
                # Pinned to one replica: the rate governor, degradation monitors and active calls
                # are in-process state. Scaling out requires moving them to a shared store first.
                "scale": {"minReplicas": 1, "maxReplicas": 1},
                # SQLite cannot acquire byte-range locks over SMB, so opening the suppression
                # database on a default Azure Files mount fails with "database is locked". nobrl
                # disables those locks, which is safe only because this app is pinned to a single
                # replica and therefore has exactly one writer.
                "volumes": [{
                    "name": "state",
                    "storageType": "AzureFile",
                    "storageName": os.environ.get("AGENT_STORAGE_NAME", "agentstate"),
                    "mountOptions": "nobrl,dir_mode=0777,file_mode=0777,uid=0,gid=0,mfsymlinks",
                }],
            },
        },
    }
    json.dump(app, sys.stdout, indent=2)  # valid YAML: az accepts JSON here


if __name__ == "__main__":
    main()
