#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/pi/openclaw-casaos"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
BOOTSTRAP_CONFIG="$APP_DIR/openclaw.bootstrap.json"
DATA_DIR="/DATA/AppData/openclaw"
TARGET_CONFIG="$DATA_DIR/openclaw.json"

if [[ "${EUID}" -eq 0 ]]; then
  DOCKER_CMD=(docker)
else
  DOCKER_CMD=(sudo docker)
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO_CMD=()
else
  SUDO_CMD=(sudo)
fi

HAS_DOCKER_COMPOSE="false"
HAS_DOCKER_COMPOSE_LEGACY="false"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE"
  exit 1
fi

if [[ ! -f "$BOOTSTRAP_CONFIG" ]]; then
  echo "Bootstrap config not found: $BOOTSTRAP_CONFIG"
  exit 1
fi

if "${DOCKER_CMD[@]}" compose version >/dev/null 2>&1; then
  HAS_DOCKER_COMPOSE="true"
elif command -v docker-compose >/dev/null 2>&1; then
  HAS_DOCKER_COMPOSE_LEGACY="true"
fi

echo "[1/4] Stop and remove broken CasaOS OpenClaw container if exists"
"${DOCKER_CMD[@]}" rm -f openclaw >/dev/null 2>&1 || true

echo "[2/4] Remove incompatible image tag if exists"
"${DOCKER_CMD[@]}" rmi icewhaletech/openclaw:2026.5.7 >/dev/null 2>&1 || true

echo "[3/4] Pull arm64-capable upstream image"
"${DOCKER_CMD[@]}" pull --platform linux/arm64 openclaw/openclaw:latest

echo "[3.5/4] Seed initial OpenClaw gateway config if missing"
"${SUDO_CMD[@]}" mkdir -p "$DATA_DIR"
if [[ ! -f "$TARGET_CONFIG" ]]; then
  "${SUDO_CMD[@]}" cp "$BOOTSTRAP_CONFIG" "$TARGET_CONFIG"
  echo "Created initial config: $TARGET_CONFIG"
else
  echo "Keeping existing config: $TARGET_CONFIG"
fi

echo "[4/4] Start OpenClaw"

# Parse IMMICH vars from docker-compose.yml for the docker run fallback
_parse_compose_env() {
  local key="$1"
  grep -E "^\s+${key}:" "$COMPOSE_FILE" | head -1 | sed 's/.*: *//' | tr -d '"'\''[:space:]'
}
IMMICH_URL_VAL="$(_parse_compose_env IMMICH_URL)"
IMMICH_API_KEY_VAL="$(_parse_compose_env IMMICH_API_KEY)"

if [[ "$HAS_DOCKER_COMPOSE" == "true" ]]; then
  echo "Using: docker compose"
  "${DOCKER_CMD[@]}" compose -f "$COMPOSE_FILE" up -d
elif [[ "$HAS_DOCKER_COMPOSE_LEGACY" == "true" ]]; then
  echo "Using: docker-compose"
  "${SUDO_CMD[@]}" docker-compose -f "$COMPOSE_FILE" up -d
else
  echo "Compose command not found, fallback to plain docker run"
  "${DOCKER_CMD[@]}" run -d \
    --name openclaw \
    --restart unless-stopped \
    --init \
    --platform linux/arm64 \
    --add-host host.docker.internal:host-gateway \
    -e HOME=/home/node \
    -e OPENCLAW_HOME=/home/node \
    -e TERM=xterm-256color \
    -e OPENCLAW_GATEWAY_TOKEN=casaos \
    -e TZ=Asia/Shanghai \
    -e IMMICH_URL="${IMMICH_URL_VAL:-http://10.55.84.133:2283}" \
    -e IMMICH_API_KEY="${IMMICH_API_KEY_VAL:-}" \
    -p 24190:18789 \
    -p 18790:18790 \
    -v /DATA/AppData/openclaw:/home/node/.openclaw \
    -v /home/pi/nas_share:/nas_share \
    openclaw/openclaw:latest \
    /bin/bash -lc 'node dist/index.js gateway --bind lan --allow-unconfigured --port 18789'
fi

echo "Done. Check status with: sudo docker ps | grep -i openclaw"
echo "Open URL: https://<your-host-ip>:24190/#token=casaos"
