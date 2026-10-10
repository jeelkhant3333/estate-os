#!/usr/bin/env bash
# Deploys the CRM, the voice agent, the knowledge service (API + worker) and the CRM web UI to Azure
# Container Apps, wired to each other and to a managed PostgreSQL server (with pgvector).
#
#   ./deploy.sh            full deploy (safe to re-run; every step is idempotent)
#   ./deploy.sh images     rebuild and roll out new images only
#   ./deploy.sh agent      rebuild and roll out the voice agent only
#   ./deploy.sh urls       print the deployed URLs
#
# Images are built by ACR Tasks in the cloud, so no local Docker, JDK or Maven is needed.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
CRM_DIR="$ROOT/real-estate-ai-mvp"
AGENT_DIR="$ROOT/voice-agent"

[[ -f azure.env ]] || { echo "Missing azure.env. Copy azure.env.example and fill it in." >&2; exit 1; }
set -a; . ./azure.env; set +a

: "${PREFIX:?}" "${LOCATION:?}" "${RESOURCE_GROUP:?}"
[[ "$PREFIX" =~ ^[a-z0-9]{3,12}$ ]] || { echo "PREFIX must be 3-12 lowercase letters/digits" >&2; exit 1; }

ACR="${PREFIX}acr"
# A subscription may allow only one Container Apps environment per region; set
# CONTAINERAPP_ENV_NAME / CONTAINERAPP_ENV_RG in azure.env to deploy into an existing one.
ENVIRONMENT="${CONTAINERAPP_ENV_NAME:-${PREFIX}-env}"
ENV_RG="${CONTAINERAPP_ENV_RG:-$RESOURCE_GROUP}"
# Environment storage names are per environment; prefixed so a shared environment never collides.
AGENT_STORAGE_NAME="${PREFIX}state"
RAG_STORAGE_NAME="${PREFIX}rag"
PG="${PREFIX}-pg"
STORAGE="${PREFIX}storage"
SHARE="agent-state"
RAG_SHARE="rag-sources"
RAG_API_APP="${PREFIX}-rag-api"
RAG_WORKER_APP="${PREFIX}-rag-worker"
CRM_APP="${PREFIX}-crm-api"
AGENT_APP="${PREFIX}-voice-agent"
WEB_APP="${PREFIX}-crm-web"
TAG="$(date +%Y%m%d%H%M%S)"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
gen() { openssl rand -base64 32 | tr -d '/+=' | cut -c1-32; }

# Generated secrets are written back to azure.env so re-runs stay stable.
persist() {
  local key="$1" value="$2"
  if grep -q "^${key}=" azure.env; then
    python3 - "$key" "$value" <<'PY'
import pathlib, sys
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path("azure.env")
lines = [f"{key}={value}" if l.startswith(f"{key}=") else l.rstrip("\n") for l in p.read_text().splitlines()]
p.write_text("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> azure.env
  fi
}

for key in JWT_SECRET SERVICE_ACCOUNT_PASSWORD VOICE_AGENT_API_KEY DEMO_PASSWORD POSTGRES_ADMIN_PASSWORD \
           RAG_SERVICE_TOKEN RAG_VOICE_TOKEN; do
  if [[ -z "${!key:-}" ]]; then
    value="$(gen)"
    printf -v "$key" '%s' "$value"
    export "${key?}"
    persist "$key" "$value"
    echo "Generated $key (saved to azure.env)"
  fi
done

fqdn() { az containerapp show -n "$1" -g "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true; }

# ---------------------------------------------------------------- infrastructure

provision() {
  log "Resource group $RESOURCE_GROUP"
  az group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none

  log "Container registry $ACR"
  az acr show -n "$ACR" -g "$RESOURCE_GROUP" -o none 2>/dev/null ||
    az acr create -n "$ACR" -g "$RESOURCE_GROUP" --sku Basic --admin-enabled true -o none

  log "PostgreSQL $PG"
  if ! az postgres flexible-server show -n "$PG" -g "$RESOURCE_GROUP" -o none 2>/dev/null; then
    az postgres flexible-server create -n "$PG" -g "$RESOURCE_GROUP" -l "$LOCATION" \
      --admin-user "$POSTGRES_ADMIN_USER" --admin-password "$POSTGRES_ADMIN_PASSWORD" \
      --tier "${POSTGRES_TIER:-Burstable}" --sku-name "${POSTGRES_SKU:-Standard_B1ms}" \
      --storage-size "${POSTGRES_STORAGE_GB:-32}" --version 16 \
      --public-access 0.0.0.0 --yes -o none
  fi
  # Kept outside the guard above: if the server exists but the database was never created,
  # nesting this would skip it on every subsequent run.
  az postgres flexible-server db show -g "$RESOURCE_GROUP" -s "$PG" -n estraos -o none 2>/dev/null ||
    az postgres flexible-server db create -g "$RESOURCE_GROUP" -s "$PG" -n estraos -o none
  # The knowledge service has its own database and needs pgvector and pg_trgm, which Azure only
  # lets a database create once they are allow-listed on the server.
  az postgres flexible-server parameter set -g "$RESOURCE_GROUP" -s "$PG" \
    --name azure.extensions --value VECTOR,PG_TRGM -o none
  az postgres flexible-server db show -g "$RESOURCE_GROUP" -s "$PG" -n estraos_knowledge -o none 2>/dev/null ||
    az postgres flexible-server db create -g "$RESOURCE_GROUP" -s "$PG" -n estraos_knowledge -o none

  log "Storage for the agent's durable state"
  # The agent keeps its do-not-call suppression list and outbound-call registry in SQLite.
  # Container Apps replicas have ephemeral disks, so that state lives on an Azure Files share.
  az storage account show -n "$STORAGE" -g "$RESOURCE_GROUP" -o none 2>/dev/null ||
    az storage account create -n "$STORAGE" -g "$RESOURCE_GROUP" -l "$LOCATION" \
      --sku Standard_LRS --kind StorageV2 -o none
  local key
  key="$(az storage account keys list -n "$STORAGE" -g "$RESOURCE_GROUP" --query '[0].value' -o tsv)"
  az storage share-rm create --storage-account "$STORAGE" -g "$RESOURCE_GROUP" \
    -n "$SHARE" --quota 8 -o none 2>/dev/null || true
  # Uploaded knowledge sources: written by the RAG API, read by the RAG worker.
  az storage share-rm create --storage-account "$STORAGE" -g "$RESOURCE_GROUP" \
    -n "$RAG_SHARE" --quota 32 -o none 2>/dev/null || true

  log "Container Apps environment $ENVIRONMENT"
  az containerapp env show -n "$ENVIRONMENT" -g "$ENV_RG" -o none 2>/dev/null ||
    az containerapp env create -n "$ENVIRONMENT" -g "$ENV_RG" -l "$LOCATION" -o none
  az containerapp env storage set -n "$ENVIRONMENT" -g "$ENV_RG" \
    --storage-name "$AGENT_STORAGE_NAME" --azure-file-account-name "$STORAGE" --azure-file-account-key "$key" \
    --azure-file-share-name "$SHARE" --access-mode ReadWrite -o none
  az containerapp env storage set -n "$ENVIRONMENT" -g "$ENV_RG" \
    --storage-name "$RAG_STORAGE_NAME" --azure-file-account-name "$STORAGE" --azure-file-account-key "$key" \
    --azure-file-share-name "$RAG_SHARE" --access-mode ReadWrite -o none
}

# ---------------------------------------------------------------- images

# Images are built locally and pushed. ACR Tasks (cloud builds) is blocked by default on new
# pay-as-you-go subscriptions, and Container Apps only runs linux/amd64, so everything is
# cross-built for that platform regardless of this machine's architecture.
push_image() {
  local name="$1" context="$2"; shift 2
  docker buildx build --platform linux/amd64 \
    -t "$ACR_SERVER/$name:$TAG" -t "$ACR_SERVER/$name:latest" "$@" --push "$context"
}

# The jar and the web bundle are built natively here rather than inside an emulated amd64
# container, which would be dramatically slower. Both artifacts are platform-independent.
build_crm_image() {
  log "Building CRM API jar"
  local java21=/opt/homebrew/opt/openjdk@21
  [[ -d "$java21" ]] && export JAVA_HOME="$java21" && export PATH="$java21/bin:$PATH"
  # clean, not just package: a stale or duplicated .class left in target/ makes the Spring Boot
  # repackage step fail with "Unable to find a single main class".
  (cd "$CRM_DIR/backend" && mvn -B -q -DskipTests clean package)

  log "Packaging and pushing CRM API image"
  local stage; stage="$(mktemp -d)"
  cp "$CRM_DIR"/backend/target/estraos-api-*.jar "$stage/app.jar"
  cat > "$stage/Dockerfile" <<'DOCKER'
FROM eclipse-temurin:21-jre-alpine
RUN addgroup -S estraos && adduser -S estraos -G estraos
WORKDIR /app
COPY --chown=estraos:estraos app.jar app.jar
USER estraos
EXPOSE 8080
ENTRYPOINT ["java","-jar","app.jar"]
DOCKER
  push_image crm-api "$stage"
  rm -rf "$stage"
}

build_agent_image() {
  log "Building voice agent image (linux/amd64)"
  push_image voice-agent "$AGENT_DIR"
}

build_rag_image() {
  log "Building knowledge service image (linux/amd64, without the local reranker)"
  push_image rag "$CRM_DIR/rag" --build-arg INSTALL_RERANKER=false
}

build_web_image() {
  # The browser calls the CRM API directly, so its URL is baked into the bundle at build time.
  local api_url="$1"
  log "Building CRM web bundle against $api_url"
  (cd "$CRM_DIR/frontend" && npm ci --silent && VITE_API_URL="${api_url}/api/v1" npm run build)

  log "Packaging and pushing CRM web image"
  local stage; stage="$(mktemp -d)"
  cp -R "$CRM_DIR/frontend/dist" "$stage/dist"
  cp "$CRM_DIR/frontend/nginx.conf" "$stage/nginx.conf"
  cat > "$stage/Dockerfile" <<'DOCKER'
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY dist /usr/share/nginx/html
EXPOSE 8080
DOCKER
  push_image crm-web "$stage"
  rm -rf "$stage"
}

# ---------------------------------------------------------------- apps

acr_creds() {
  ACR_SERVER="$(az acr show -n "$ACR" -g "$RESOURCE_GROUP" --query loginServer -o tsv)"
  ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
  ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"
  docker info >/dev/null 2>&1 || { echo "Docker must be running to build images." >&2; exit 1; }
  echo "$ACR_PASS" | docker login "$ACR_SERVER" -u "$ACR_USER" --password-stdin >/dev/null
}

# Inside the environment, an app is reachable at http://<app name> (internal ingress, port 80).
RAG_INTERNAL_URL="http://${RAG_API_APP}"

deploy_rag() {
  log "Deploying knowledge service (API + worker)"
  ENV_ID="$(az containerapp env show -n "$ENVIRONMENT" -g "$ENV_RG" --query id -o tsv)"
  export RAG_STORAGE_NAME
  export ENV_ID ACR_SERVER ACR_USER ACR_PASS TAG LOCATION RAG_SERVICE_TOKEN RAG_VOICE_TOKEN OPENAI_API_KEY
  export RAG_VOICE_WORKSPACE_ID="${SERVICE_ACCOUNT_WORKSPACE_ID:-1}"
  export RAG_DATABASE_URL="postgresql://${POSTGRES_ADMIN_USER}:${POSTGRES_ADMIN_PASSWORD}@${PG}.postgres.database.azure.com:5432/estraos_knowledge?sslmode=require"
  local role app yaml
  for role in api worker; do
    app="$RAG_API_APP"; [[ "$role" == worker ]] && app="$RAG_WORKER_APP"
    yaml="$(mktemp -t "rag-$role")"
    python3 render_rag.py "$role" > "$yaml"
    if az containerapp show -n "$app" -g "$RESOURCE_GROUP" -o none 2>/dev/null; then
      az containerapp update -n "$app" -g "$RESOURCE_GROUP" --yaml "$yaml" -o none
    else
      az containerapp create -n "$app" -g "$RESOURCE_GROUP" --yaml "$yaml" -o none
    fi
    rm -f "$yaml"
  done
}

deploy_crm() {
  log "Deploying CRM API"
  local jdbc="jdbc:postgresql://${PG}.postgres.database.azure.com:5432/estraos?sslmode=require"
  local secrets=(
    "db-password=$POSTGRES_ADMIN_PASSWORD" "jwt-secret=$JWT_SECRET"
    "service-password=$SERVICE_ACCOUNT_PASSWORD" "voice-key=$VOICE_AGENT_API_KEY"
    "demo-password=$DEMO_PASSWORD" "rag-token=$RAG_SERVICE_TOKEN"
  )
  local env=(
    "DATABASE_URL=$jdbc" "DATABASE_USERNAME=$POSTGRES_ADMIN_USER"
    "DATABASE_PASSWORD=secretref:db-password" "JWT_SECRET=secretref:jwt-secret"
    "SPRING_PROFILES_ACTIVE=${SPRING_PROFILES_ACTIVE:-}" "DEMO_PASSWORD=secretref:demo-password"
    "SERVICE_ACCOUNT_EMAIL=$SERVICE_ACCOUNT_EMAIL"
    "SERVICE_ACCOUNT_PASSWORD=secretref:service-password"
    "SERVICE_ACCOUNT_WORKSPACE_ID=$SERVICE_ACCOUNT_WORKSPACE_ID"
    "SERVICE_ACCOUNT_WORKSPACE_NAME=$SERVICE_ACCOUNT_WORKSPACE_NAME"
    "SERVICE_ACCOUNT_ADMIN_EMAILS=${SERVICE_ACCOUNT_ADMIN_EMAILS:-}"
    # mock keeps the optional notification adapter offline; the knowledge service and the voice
    # agent are used for real because their URLs are set.
    "INTEGRATIONS_MODE=mock" "VOICE_AGENT_API_KEY=secretref:voice-key"
    "RAG_SERVICE_URL=$RAG_INTERNAL_URL" "RAG_SERVICE_TOKEN=secretref:rag-token"
    # Placeholder replaced by wire_together once the agent's FQDN exists.
    "VOICE_AGENT_SERVICE_URL=${VOICE_AGENT_SERVICE_URL:-https://placeholder.invalid}"
    "CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS:-http://localhost:5173}"
    # The image's working directory is not writable by its user.
    "FILE_STORAGE_PATH=/tmp/files"
  )
  if az containerapp show -n "$CRM_APP" -g "$RESOURCE_GROUP" -o none 2>/dev/null; then
    az containerapp secret set -n "$CRM_APP" -g "$RESOURCE_GROUP" --secrets "${secrets[@]}" -o none
    az containerapp update -n "$CRM_APP" -g "$RESOURCE_GROUP" \
      --image "$ACR_SERVER/crm-api:$TAG" --set-env-vars "${env[@]}" -o none
  else
    az containerapp create -n "$CRM_APP" -g "$RESOURCE_GROUP" --environment "$(az containerapp env show -n "$ENVIRONMENT" -g "$ENV_RG" --query id -o tsv)" \
      --image "$ACR_SERVER/crm-api:$TAG" \
      --registry-server "$ACR_SERVER" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
      --target-port 8080 --ingress external --transport auto \
      --min-replicas 1 --max-replicas 3 --cpu 1 --memory 2Gi \
      --secrets "${secrets[@]}" --env-vars "${env[@]}" -o none
  fi
}

deploy_agent() {
  log "Deploying voice agent"
  # One replica only: the rate governor, degradation monitors and live calls are in-process state.
  # Scaling out needs that state moved to a shared store first.
  local yaml; yaml="$(mktemp -t agent-app)"
  ENV_ID="$(az containerapp env show -n "$ENVIRONMENT" -g "$ENV_RG" --query id -o tsv)"
  export ENV_ID ACR_SERVER ACR_USER ACR_PASS TAG LOCATION AGENT_STORAGE_NAME
  export CRM_SERVICE_EMAIL="${SERVICE_ACCOUNT_EMAIL:-}"
  export CRM_SERVICE_PASSWORD="${SERVICE_ACCOUNT_PASSWORD:-}"
  export CRM_WORKSPACE_ID="${SERVICE_ACCOUNT_WORKSPACE_ID:-1}"
  export CRM_BASE_URL="${CRM_BASE_URL:-https://$(fqdn "$CRM_APP")}"
  export RAG_SERVICE_URL="$RAG_INTERNAL_URL" RAG_VOICE_TOKEN
  python3 render_agent.py > "$yaml"
  if az containerapp show -n "$AGENT_APP" -g "$RESOURCE_GROUP" -o none 2>/dev/null; then
    az containerapp update -n "$AGENT_APP" -g "$RESOURCE_GROUP" --yaml "$yaml" -o none
  else
    az containerapp create -n "$AGENT_APP" -g "$RESOURCE_GROUP" --yaml "$yaml" -o none
  fi
  rm -f "$yaml"
}

deploy_web() {
  log "Deploying CRM web UI"
  if az containerapp show -n "$WEB_APP" -g "$RESOURCE_GROUP" -o none 2>/dev/null; then
    az containerapp update -n "$WEB_APP" -g "$RESOURCE_GROUP" --image "$ACR_SERVER/crm-web:$TAG" -o none
  else
    az containerapp create -n "$WEB_APP" -g "$RESOURCE_GROUP" --environment "$(az containerapp env show -n "$ENVIRONMENT" -g "$ENV_RG" --query id -o tsv)" \
      --image "$ACR_SERVER/crm-web:$TAG" \
      --registry-server "$ACR_SERVER" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
      --target-port 8080 --ingress external \
      --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory 1Gi -o none
  fi
}

# Each service needs the other's public hostname, which only exists after both are created.
wire_together() {
  log "Wiring the services to each other"
  local crm agent web
  crm="https://$(fqdn "$CRM_APP")"
  agent="https://$(fqdn "$AGENT_APP")"
  web="https://$(fqdn "$WEB_APP")"

  az containerapp update -n "$CRM_APP" -g "$RESOURCE_GROUP" --set-env-vars \
    "VOICE_AGENT_SERVICE_URL=$agent" "CORS_ALLOWED_ORIGINS=$web" -o none

  az containerapp update -n "$AGENT_APP" -g "$RESOURCE_GROUP" --set-env-vars \
    "CRM_BASE_URL=$crm" "PUBLIC_WS_BASE_URL=wss://$(fqdn "$AGENT_APP")" \
    "VOICE_LINK_WEBHOOK_URL=$agent/telephony/voicelink/webhook${VOICE_LINK_WEBHOOK_TOKEN:+?token=$VOICE_LINK_WEBHOOK_TOKEN}" -o none
}

urls() {
  cat <<EOF

  CRM web UI     https://$(fqdn "$WEB_APP")
  CRM API        https://$(fqdn "$CRM_APP")        (docs at /swagger-ui.html)
  Voice agent    https://$(fqdn "$AGENT_APP")      (health at /healthz)
  Knowledge      $RAG_INTERNAL_URL              (internal only)

  VoiceLink websocket bot URL:
    wss://$(fqdn "$AGENT_APP")/telephony/voicelink/ws
  VoiceLink webhook URL:
    https://$(fqdn "$AGENT_APP")/telephony/voicelink/webhook

EOF
}

case "${1:-all}" in
  urls) urls ;;
  agent)
    # Voice agent only: rebuild its image and roll it out.
    acr_creds; build_agent_image; deploy_agent; wire_together; urls
    ;;
  web)
    # CRM web only: rebuild the bundle against the deployed API and roll it out.
    acr_creds; build_web_image "https://$(fqdn "$CRM_APP")"; deploy_web; urls
    ;;
  crm)
    # CRM API and web only: the voice agent and knowledge service keep running untouched.
    # The apps already exist, so the CRM gets its real URLs up front instead of placeholders that
    # wire_together would fix, which would also restart the agent.
    VOICE_AGENT_SERVICE_URL="https://$(fqdn "$AGENT_APP")"
    CORS_ALLOWED_ORIGINS="https://$(fqdn "$WEB_APP")"
    acr_creds; build_crm_image; deploy_crm
    build_web_image "https://$(fqdn "$CRM_APP")"; deploy_web; urls
    ;;
  images)
    acr_creds
    build_crm_image; build_agent_image; build_rag_image
    build_web_image "https://$(fqdn "$CRM_APP")"
    deploy_rag; deploy_crm; deploy_agent; deploy_web; wire_together; urls
    ;;
  all)
    provision
    acr_creds
    build_crm_image
    build_agent_image
    build_rag_image
    deploy_rag
    deploy_crm
    deploy_agent
    # The web bundle needs the CRM's hostname, so it is built after the API exists.
    build_web_image "https://$(fqdn "$CRM_APP")"
    deploy_web
    wire_together
    urls
    echo "Secrets were written to azure.env. Keep that file out of source control."
    ;;
  *) echo "usage: $0 [all|images|crm|web|agent|urls]" >&2; exit 1 ;;
esac
