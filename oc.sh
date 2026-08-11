#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/pi/openclaw-casaos"

docker_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

usage() {
  cat <<'EOF'
Usage:
  ./oc.sh deploy
  ./oc.sh reset
  ./oc.sh status
  ./oc.sh logs [N]
  ./oc.sh health
  ./oc.sh url
  ./oc.sh model
  ./oc.sh tools-nas-setup
  ./oc.sh tools-nas-show
  ./oc.sh tools-immich-setup
  ./oc.sh tools-immich-show
  ./oc.sh pair-list
  ./oc.sh pair-approve <request_id>
EOF
}

clean_request_id() {
  printf '%s' "$1" | tr -d '[:space:]' | sed 's/[。．，,；;：:]$//'
}

case "${1:-}" in
  deploy)
    "$APP_DIR/deploy.sh"
    ;;
  reset)
    "$APP_DIR/reset-config.sh"
    docker_cmd restart openclaw
    ;;
  status)
    docker_cmd ps -a | grep -i openclaw || true
    ;;
  logs)
    docker_cmd logs --tail "${2:-120}" openclaw
    ;;
  health)
    curl -k -I --max-time 5 https://127.0.0.1:24190/healthz
    ;;
  url)
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "https://${ip:-<your-host-ip>}:24190/#token=casaos"
    ;;
  model)
    docker_cmd exec -it -e TERM=xterm-256color openclaw node dist/index.js config --section model
    ;;
  tools-nas-setup)
    # Restrict filesystem tools to NAS mount only.
    docker_cmd exec openclaw node dist/index.js mcp set nas_files '{"enabled":true,"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/nas_share"],"toolFilter":{"include":["move_file","list_directory","create_directory","search_files","get_file_info","read_file","write_file","edit_file"]}}'
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "NAS tools configured. Available file root: /nas_share"
    ;;
  tools-nas-show)
    docker_cmd exec openclaw node dist/index.js mcp show nas_files --json
    ;;
  tools-immich-setup)
    # Read env vars from running container (set in docker-compose.yml)
    IMMICH_URL_VAL="$(docker_cmd exec openclaw printenv IMMICH_URL 2>/dev/null || echo 'http://10.55.84.133:2283')"
    IMMICH_KEY_VAL="$(docker_cmd exec openclaw printenv IMMICH_API_KEY 2>/dev/null || echo '')"
    if [[ -z "${IMMICH_KEY_VAL}" ]]; then
      echo "ERROR: IMMICH_API_KEY is not set in the openclaw container."
      echo "Please set it in docker-compose.yml and run: ./oc.sh deploy"
      exit 1
    fi
    docker_cmd exec openclaw node dist/index.js mcp set immich \
      "{\"enabled\":true,\"command\":\"npx\",\"args\":[\"-y\",\"immich-mcp\"],\"env\":{\"IMMICH_URL\":\"${IMMICH_URL_VAL}\",\"IMMICH_API_KEY\":\"${IMMICH_KEY_VAL}\"}}"
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Immich MCP tools configured."
    echo "  IMMICH_URL: ${IMMICH_URL_VAL}"
    echo "You can now ask OpenClaw: 找出所有有海的照片放入旅行相册"
    ;;
  tools-immich-show)
    docker_cmd exec openclaw node dist/index.js mcp show immich --json
    ;;
  pair-list)
    docker_cmd exec -it openclaw node dist/index.js devices list
    ;;
  pair-approve)
    if [[ -z "${2:-}" ]]; then
      echo "Missing request_id"
      usage
      exit 1
    fi
    req="$(clean_request_id "$2")"
    docker_cmd exec -it openclaw node dist/index.js devices approve "$req"
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $1"
    usage
    exit 1
    ;;
esac