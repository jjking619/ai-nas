#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
APP_DIR="$SCRIPT_DIR"
if [[ ! -f "$APP_DIR/deploy.sh" && -f "/home/pi/NAS-Demo/deploy.sh" ]]; then
  APP_DIR="/home/pi/NAS-Demo"
fi

if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  source "$APP_DIR/.env"
  set +a
fi

# NAS 共享目录：可从 .env / 环境变量覆盖，默认当前用户主目录（用户名无关）
NAS_ROOT="${NAS_ROOT:-$HOME/nas_share}"
export NAS_ROOT

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
  ./oc.sh tools-media-setup
  ./oc.sh tools-media-show
  ./oc.sh tools-immich-setup
  ./oc.sh tools-immich-show
  ./oc.sh tools-kb-setup
  ./oc.sh tools-kb-show
  ./oc.sh tools-sync         Sync NAS-Demo sources -> nas_share/tools (runtime copy)
  ./oc.sh pair-list
  ./oc.sh pair-approve <request_id>
  ./oc.sh immich-apply       Apply immich-compose.yml to CasaOS
  ./oc.sh immich-show        Show current CasaOS Immich config
  ./oc.sh immich-sync-jobs   Trigger Immich ML jobs (faceDetection + smartSearch)
  ./oc.sh jellyfin-deploy    Start Jellyfin (jellyfin-compose.yml)
  ./oc.sh jellyfin-show      Show Jellyfin container status
  ./oc.sh voice-assistant-deploy  Install Voice Assistant (CasaOS web app)
  ./oc.sh voice-assistant-show    Show Voice Assistant container status
  ./oc.sh nas-files-deploy        Install NAS file browser (read-only nas_share)
  ./oc.sh nas-files-show          Show NAS file browser container status
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
    bash "$APP_DIR/install.sh" reset
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
  tools-media-setup)
    mkdir -p $NAS_ROOT/downloads/家庭影院
    mkdir -p $NAS_ROOT/tools
    cp "$APP_DIR/local_voice_chat/download_media_mcp.js" $NAS_ROOT/tools/download_media_mcp.js

    if ! docker_cmd ps --format '{{.Names}}' | grep -qx media_downloader; then
      if docker_cmd ps -a --format '{{.Names}}' | grep -qx media_downloader; then
        docker_cmd start media_downloader >/dev/null
      else
        docker_cmd build -t nas-media-downloader:local "$APP_DIR/media_downloader"
        docker_cmd run -d \
          --name media_downloader \
          --restart unless-stopped \
          -e DOWNLOAD_ROOT=/downloads \
          -e PORT=8081 \
          -e YTDLP_TIMEOUT_SEC=1800 \
          -e DOWNLOAD_TTS_TEXT="下载已完成" \
          -v $NAS_ROOT/downloads:/downloads \
          -p 28081:8081 \
          nas-media-downloader:local >/dev/null
      fi
    fi
    # 让 openclaw 能按容器名直接访问下载服务（host.docker.internal 在本机不可达）
    if docker_cmd network inspect big-bear-immich_big_bear-immich_network >/dev/null 2>&1; then
      if ! docker_cmd network inspect big-bear-immich_big_bear-immich_network \
          --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q ' media_downloader'; then
        docker_cmd network connect big-bear-immich_big_bear-immich_network media_downloader
      fi
    fi

    docker_cmd exec openclaw node dist/index.js mcp set download_media '{"enabled":true,"command":"node","args":["/nas_share/tools/download_media_mcp.js"],"env":{"DOWNLOAD_API_URL":"http://media_downloader:8081/download","DOWNLOAD_ROOT_LABEL":"'"$NAS_ROOT"'/downloads","DOWNLOAD_NOTIFY_TEXT":"下载已完成"}}'
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Media download tool configured."
    echo "Download root: $NAS_ROOT/downloads"
    ;;
  tools-media-show)
    docker_cmd exec openclaw node dist/index.js mcp show download_media --json
    ;;
  tools-immich-setup)
    # Read env vars from running container (set in docker-compose.yml)
    IMMICH_URL_VAL="$(docker_cmd exec openclaw printenv IMMICH_URL 2>/dev/null || echo 'http://immich-server:2283')"
    IMMICH_KEY_VAL="$(docker_cmd exec openclaw printenv IMMICH_API_KEY 2>/dev/null || echo '')"
    if [[ -z "${IMMICH_KEY_VAL}" ]]; then
      echo "ERROR: IMMICH_API_KEY is not set in the openclaw container."
      echo "Please set it in docker-compose.yml and run: ./oc.sh deploy"
      exit 1
    fi
    # immich-mcp requires IMMICH_BASE_URL in the form http://<host>:<port>/api
    docker_cmd exec openclaw node dist/index.js mcp set immich \
      "{\"enabled\":true,\"command\":\"npx\",\"args\":[\"-y\",\"immich-mcp\"],\"env\":{\"IMMICH_BASE_URL\":\"${IMMICH_URL_VAL}/api\",\"IMMICH_API_KEY\":\"${IMMICH_KEY_VAL}\"}}"
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Immich MCP tools configured."
    echo "  IMMICH_BASE_URL: ${IMMICH_URL_VAL}/api"
    echo "You can now ask OpenClaw: 找出所有有海的照片放入旅行相册"
    ;;
  tools-immich-show)
    docker_cmd exec openclaw node dist/index.js mcp show immich --json
    ;;
  tools-kb-setup)
    mkdir -p $NAS_ROOT/tools
    mkdir -p $NAS_ROOT/knowledge_base_data
    cp "$APP_DIR/knowledge_base/kb_mcp.js" $NAS_ROOT/tools/kb_mcp.js

    if ! docker_cmd ps --format '{{.Names}}' | grep -qx knowledge_base; then
      if docker_cmd ps -a --format '{{.Names}}' | grep -qx knowledge_base; then
        docker_cmd start knowledge_base >/dev/null
      else
        docker_cmd build -t nas-knowledge-base:local "$APP_DIR/knowledge_base"
        docker_cmd run -d \
          --name knowledge_base \
          --restart unless-stopped \
          -e NAS_ROOT=/nas_share \
          -e PORT=8084 \
          -e SCAN_INTERVAL=60 \
          -v $NAS_ROOT:/nas_share:ro \
          -v $NAS_ROOT/knowledge_base_data:/data \
          -p 28084:8084 \
          nas-knowledge-base:local >/dev/null
      fi
    fi

    # 让 openclaw 能按容器名直接访问 KB 服务（host.docker.internal 在本机不可达）
    if docker_cmd network inspect big-bear-immich_big_bear-immich_network >/dev/null 2>&1; then
      if ! docker_cmd network inspect big-bear-immich_big_bear-immich_network \
          --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q ' knowledge_base'; then
        docker_cmd network connect big-bear-immich_big_bear-immich_network knowledge_base
      fi
    fi

    IMMICH_URL_VAL="$(docker_cmd exec openclaw printenv IMMICH_URL 2>/dev/null || echo 'http://immich-server:2283')"
    IMMICH_KEY_VAL="$(docker_cmd exec openclaw printenv IMMICH_API_KEY 2>/dev/null || echo '')"

    docker_cmd exec openclaw node dist/index.js mcp set kb_search \
      "{\"enabled\":true,\"command\":\"node\",\"args\":[\"/nas_share/tools/kb_mcp.js\"],\"env\":{\"KB_API_URL\":\"http://knowledge_base:8084\",\"IMMICH_BASE_URL\":\"${IMMICH_URL_VAL}\",\"IMMICH_API_KEY\":\"${IMMICH_KEY_VAL}\"}}"
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Knowledge base configured."
    echo "  KB API:   http://knowledge_base:8084"
    echo "  Indexed:  /nas_share (excludes tools/ Immich上传/)"
    echo "  Photos:   Immich CLIP (${IMMICH_URL_VAL})"
    ;;
  tools-kb-show)
    docker_cmd exec openclaw node dist/index.js mcp show kb_search --json
    ;;
  tools-sync)
    # 统一同步：NAS-Demo（git 唯一源码）→ nas_share/tools（容器运行副本）
    # 并清理遗留副本；MCP 脚本有变更时重启 openclaw 使配置生效。
    mkdir -p $NAS_ROOT/tools

    synced=0
    changed=0
    for entry in \
      "local_voice_chat/download_media_mcp.js" \
      "local_voice_chat/image_batch.py" \
      "local_voice_chat/nas_classify.py" \
      "knowledge_base/kb_mcp.js"; do
      src="$APP_DIR/$entry"
      dst="$NAS_ROOT/tools/$(basename "$entry")"
      if [[ ! -f "$src" ]]; then
        echo "SKIP  $entry (源不存在)"
        continue
      fi
      if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        echo "SAME  $entry"
        continue
      fi
      cp "$src" "$dst"
      echo "SYNC  $entry -> $dst"
      synced=$((synced + 1))
      case "$entry" in
        *download_media_mcp.js|*kb_mcp.js) changed=1 ;;
      esac
    done

    # 清理遗留副本（实际运行在 NAS-Demo，nas_share/tools 内无人使用）
    for legacy in hotwords.txt voice_bridge.py; do
      if [[ -f "$NAS_ROOT/tools/$legacy" ]]; then
        rm -f "$NAS_ROOT/tools/$legacy"
        echo "RM    $legacy (遗留副本，已清理)"
      fi
    done

    if [[ "$changed" -eq 1 ]]; then
      docker_cmd exec openclaw node dist/index.js mcp reload || true
      docker_cmd restart openclaw
      echo "MCP 脚本有变更，已重启 openclaw"
    else
      echo "MCP 脚本无变更，无需重启 openclaw"
    fi
    echo "tools-sync done: $synced file(s) synced"
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
  immich-apply)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
    IMMICH_COMPOSE="${SCRIPT_DIR}/immich-compose.yml"
    if [[ ! -f "${IMMICH_COMPOSE}" ]]; then
      echo "ERROR: ${IMMICH_COMPOSE} not found"
      exit 1
    fi
    echo "Applying ${IMMICH_COMPOSE} to CasaOS..."
    casaos-cli app-management apply big-bear-immich -f "${IMMICH_COMPOSE}"
    echo "Done. CasaOS Immich config updated (changes applied asynchronously)."
    ;;
  immich-show)
    casaos-cli app-management show local big-bear-immich --yaml 2>&1
    ;;
  immich-sync-jobs)
    API="http://127.0.0.1:2283/api"
    KEY="$(grep -o 'IMMICH_API_KEY[^,]*' "${BASH_SOURCE%/*}/docker-compose.yml" | cut -d: -f2 | tr -d ' "' | head -1 2>/dev/null || echo '')"
    if [[ -z "${KEY}" ]]; then
      echo "ERROR: cannot read IMMICH_API_KEY from docker-compose.yml"
      exit 1
    fi
    echo "Triggering faceDetection..."
    curl -s -X PUT "${API}/jobs/faceDetection" \
      -H "x-api-key: ${KEY}" -H "Content-Type: application/json" \
      -d '{"command":"start","force":false}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('faceDetection active:',d.get('queueStatus',{}).get('isActive'))"
    echo "Triggering smartSearch (CLIP)..."
    curl -s -X PUT "${API}/jobs/smartSearch" \
      -H "x-api-key: ${KEY}" -H "Content-Type: application/json" \
      -d '{"command":"start","force":false}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('smartSearch active:',d.get('queueStatus',{}).get('isActive'))"
    echo "Jobs triggered. New photos will be indexed shortly."
    ;;
  jellyfin-deploy)
    JELLYFIN_COMPOSE="${APP_DIR}/jellyfin-compose.yml"
    if [[ ! -f "${JELLYFIN_COMPOSE}" ]]; then
      echo "ERROR: ${JELLYFIN_COMPOSE} not found"
      exit 1
    fi
    casaos-cli app-management install -f "${JELLYFIN_COMPOSE}"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "Jellyfin installed to CasaOS (asynchronous). Open http://${ip:-<your-host-ip>}:8096"
    echo "First run: 建库时选 /media 下的子目录，建议关闭 Admin > Playback > Transcoding"
    ;;
  jellyfin-show)
    docker_cmd ps -a --filter name=jellyfin --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  voice-assistant-deploy)
    VOICE_COMPOSE="${APP_DIR}/voice-assistant-compose.yml"
    if [[ ! -f "${VOICE_COMPOSE}" ]]; then
      echo "ERROR: ${VOICE_COMPOSE} not found"
      exit 1
    fi
    casaos-cli app-management install -f "${VOICE_COMPOSE}"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "Voice Assistant installed to CasaOS (asynchronous). Open http://${ip:-<your-host-ip>}:28083"
    ;;
  voice-assistant-show)
    docker_cmd ps -a --filter name=voice_assistant --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  nas-files-deploy)
    FB_COMPOSE="${APP_DIR}/filebrowser-compose.yml"
    if [[ ! -f "${FB_COMPOSE}" ]]; then
      echo "ERROR: ${FB_COMPOSE} not found"
      exit 1
    fi
    # 预先创建 config/database 目录（容器以 uid 1001 运行，目录需可写）
    mkdir -p /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    chmod 777 /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    casaos-cli app-management install -f "${FB_COMPOSE}"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "NAS file browser installing (asynchronous)..."
    # 免登录：等数据库初始化后，停止容器 -> 写入 noauth -> 再启动（读写由 uid 1001 与可写挂载保证）
    for _ in $(seq 1 40); do
      if [ -f /DATA/AppData/filebrowser/database/filebrowser.db ]; then
        docker_cmd stop filebrowser >/dev/null 2>&1 || true
        docker_cmd run --rm --user 1001:1001 --entrypoint /bin/filebrowser \
          -v /DATA/AppData/filebrowser/database:/database \
          filebrowser/filebrowser:latest \
          -d /database/filebrowser.db config set --auth.method=noauth >/dev/null 2>&1 || true
        docker_cmd start filebrowser >/dev/null 2>&1 || true
        echo "Auth disabled (no login)."
        break
      fi
      sleep 2
    done
    echo "Open http://${ip:-<your-host-ip>}:28085"
    ;;
  nas-files-show)
    docker_cmd ps -a --filter name=filebrowser --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
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