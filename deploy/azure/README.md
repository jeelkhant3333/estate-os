# Deploying to Azure

Three containers on Azure Container Apps, one managed PostgreSQL server, one Azure Files share:

```
                 browser
                    │
                    ▼
        ┌──────────────────────┐
        │  <prefix>-crm-web    │  React UI (nginx)
        └──────────┬───────────┘
                   │ HTTPS, API URL baked in at build time
                   ▼
        ┌──────────────────────┐        ┌──────────────────────────┐
        │  <prefix>-crm-api    │◄──────►│  <prefix>-voice-agent    │
        │  Spring Boot         │        │  FastAPI + websocket     │
        └──────────┬───────────┘        └────────────┬─────────────┘
                   │                                 │
                   ▼                                 ▼
        Azure Database for              Azure Files share
        PostgreSQL (estraos)           (do-not-call list, call registry)
```

The two services call each other over their public hostnames:

- **CRM → agent** with `VOICE_AGENT_API_KEY` as a bearer token, to start calls and fetch details.
- **Agent → CRM** as a service account, logging in through the CRM's own `/api/v1/auth/login`.

## First deploy

You need the Azure CLI, logged in (`az login`) with a subscription selected. You do **not** need
Docker, a JDK or Maven locally — images are built in Azure by ACR Tasks.

```bash
cd deploy/azure
cp azure.env.example azure.env
$EDITOR azure.env          # set PREFIX, LOCATION, provider keys
./deploy.sh
```

Leave `JWT_SECRET`, `SERVICE_ACCOUNT_PASSWORD`, `VOICE_AGENT_API_KEY`, `DEMO_PASSWORD` and
`POSTGRES_ADMIN_PASSWORD` blank and the script generates them, writes them back into `azure.env`
and prints them once. **`azure.env` then holds live credentials — keep it out of source control.**

The run takes roughly 15–25 minutes, most of it the PostgreSQL server and the first image builds.
It ends by printing the three URLs plus the VoiceLink websocket and webhook URLs.

Re-running `./deploy.sh` is safe. To ship code changes only:

```bash
./deploy.sh images
```

## What happens on first boot

1. Flyway applies `V1__schema.sql` to the empty database.
2. `ServiceAccountProvisioner` creates the workspace named by `SERVICE_ACCOUNT_WORKSPACE_NAME`
   (only when `SERVICE_ACCOUNT_WORKSPACE_ID` does not already exist), then creates the voice
   agent's login and makes it a `MANAGER` of that workspace.
3. With `SPRING_PROFILES_ACTIVE=demo`, `DemoSeed` additionally creates a separate demo workspace
   with fictional projects and leads. Set it empty for a clean tenant.

## Going live on telephony

`PROVIDER_MODE=fake` deploys a working system that places no calls and needs no provider keys —
use it to check the wiring end to end. For real calls:

1. Put the Sarvam and Deepgram keys plus the VoiceLink credentials in `azure.env`.
2. Set `PROVIDER_MODE=live`.
3. `./deploy.sh images`
4. In the VoiceLink console, point the websocket bot at the printed
   `wss://…/telephony/voicelink/ws` and the webhook at the printed `…/telephony/voicelink/webhook`.

Check `GET https://<agent>/healthz` before the first call: it reports provider mode, the LLM and
STT circuit-breaker states and whether the outbound queue is paused.

## Knowledge service

`<prefix>-rag-api` (internal ingress only) and `<prefix>-rag-worker` run the same image. They use a
second database, `estraos_knowledge`, on the same server; `deploy.sh` allow-lists `vector` and
`pg_trgm` (`azure.extensions`) and the API applies its migrations on start. Uploaded sources live
on the `rag-sources` Azure Files share. The CRM and the agent reach it at `http://<prefix>-rag-api`.

## One environment per region

Some subscriptions allow a single Container Apps environment per region. Set
`CONTAINERAPP_ENV_NAME` and `CONTAINERAPP_ENV_RG` in `azure.env` to deploy into an existing one;
everything else (database, registry, storage, apps) stays in `RESOURCE_GROUP`, and the environment
storage names are prefixed so they never collide with other apps in that environment.

## Things worth knowing before this carries real traffic

- **The agent's SQLite stores use the rollback journal on Azure Files** (`SQLITE_JOURNAL_MODE=DELETE`).
  WAL needs shared memory that the old and new replica cannot share over SMB, so a restart never
  completed. A deployment still running in WAL mode needs its old revision deactivated once
  (`az containerapp revision deactivate`) when this setting is first rolled out.

- **The agent runs as a single replica, pinned.** Its rate governor, degradation monitors and
  active-call table are in-process. `maxReplicas` is 1 on purpose; raising it will double-count
  rate limits and strand calls. Moving that state to Redis is the prerequisite for scaling out.
- **The agent's durable state is one Azure Files share.** The do-not-call suppression list lives
  there. Back it up; losing it means re-calling people who asked not to be called.
- **Postgres is deployed with public network access restricted to Azure services.** For production,
  move it behind a private endpoint and put the Container Apps environment on a VNet.
- **Secrets are Container Apps secrets**, not Key Vault. For a regulated deployment, switch the
  apps to managed identity plus Key Vault references.
- **Cost**: roughly the Burstable B1ms database plus three always-on Container Apps replicas.
  The agent and CRM API cannot scale to zero (`minReplicas: 1`) because both hold warm state.
