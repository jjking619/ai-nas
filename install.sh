#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# install.sh - NAS-Demo 一键安装/配置入口
#
# 目标：用户只需要配置 OpenClaw 的模型 API（base URL + API key + model id）
# 其余步骤自动完成，并尽量复用项目现有脚本，保持简洁。
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
APP_DIR="$SCRIPT_DIR"
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

API_KEY_ARG=""
BASE_URL_ARG=""
MODEL_ID_ARG=""
CHECK_ONLY=0
RESET=0
SKIP_FIREWALL=0
ARG_FIREWALL_ONLY=0

# NAS-Demo 需要放行的 TCP 端口（INPUT 链，宿主机/局域网访问）。
# 可用环境变量 FIREWALL_PORTS 覆盖，例如:
#   FIREWALL_PORTS="22 80 24190" bash install.sh
FIREWALL_PORTS="${FIREWALL_PORTS:-80 2283 8096 28081 28082 28083 28084 28085 24190 24192}"

# 代码仓库地址（仅在“从任意位置运行、目录里还没有 NAS-Demo 代码”时用于自动 clone）
INSTALL_REPO="${INSTALL_REPO:-https://github.com/jjking619/ai-nas.git}"

usage() {
  cat <<'EOF'
用法:
  bash install.sh
  bash install.sh --api-key=sk-xxx --model-base-url=https://api.deepseek.com/v1 --model-id=deepseek-chat
  bash install.sh --check
  bash install.sh reset            # 重置 OpenClaw 配置为 bootstrap 并重启容器
  bash install.sh firewall         # 仅放行 NAS-Demo 所需端口（幂等 + 持久化）

说明:
  - 交互模式下只会询问 OpenClaw API 相关配置
  - 非交互可通过参数传入，便于远程或自动化
  - --check 仅做环境检查，不会改动系统
  - --skip-firewall 在正常安装时跳过自动放行端口
  - 若在“还没有 NAS-Demo 代码”的目录运行，会自动 git clone（可用 --repo 换地址）
EOF
}

for arg in "$@"; do
  case "$arg" in
    --api-key=*) API_KEY_ARG="${arg#*=}" ;;
    --model-base-url=*) BASE_URL_ARG="${arg#*=}" ;;
    --model-id=*) MODEL_ID_ARG="${arg#*=}" ;;
    --check) CHECK_ONLY=1 ;;
    --skip-firewall) SKIP_FIREWALL=1 ;;
    --repo=*) INSTALL_REPO="${arg#*=}" ;;
    reset) RESET=1 ;;
    firewall) RESET=0; CHECK_ONLY=0; ARG_FIREWALL_ONLY=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *)
      echo "未知参数: $arg" >&2
      usage
      exit 1
      ;;
  esac
done

log() { echo "[install] $*"; }
warn() { echo "[install][WARN] $*" >&2; }
die() { echo "[install][ERROR] $*" >&2; exit 1; }

docker_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

container_exists() {
  docker_cmd ps -a --format '{{.Names}}' | grep -qx "$1"
}

env_get() {
  local key="$1"
  grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

env_set() {
  local key="$1"
  local value="$2"
  if grep -qE "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

mask_key() {
  local s="$1"
  if [[ -z "$s" ]]; then
    echo "(空)"
  else
    echo "${s:0:6}..."
  fi
}

check_env() {
  log "环境检查中..."
  require_cmd bash
  require_cmd curl
  require_cmd openssl
  require_cmd python3
  require_cmd docker
  if ! docker_cmd ps >/dev/null 2>&1; then
    die "docker 无法访问，请确认当前用户有 sudo docker 权限"
  fi
  if [[ -f "$APP_DIR/oc.sh" ]]; then
    chmod +x "$APP_DIR/oc.sh" || true
  fi
  log "环境检查通过"
}

prepare_env() {
  [[ -f "$ENV_EXAMPLE" ]] || die "缺少 $ENV_EXAMPLE"
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    log "已创建 .env"
  fi

  local base_url api_key model_id token
  base_url="$(env_get OPENCLAW_MODEL_BASE_URL)"
  api_key="$(env_get OPENCLAW_MODEL_API_KEY)"
  model_id="$(env_get OPENCLAW_MODEL_ID)"
  token="$(env_get OPENCLAW_GATEWAY_TOKEN)"

  [[ -n "$BASE_URL_ARG" ]] && base_url="$BASE_URL_ARG"
  [[ -n "$API_KEY_ARG" ]] && api_key="$API_KEY_ARG"
  [[ -n "$MODEL_ID_ARG" ]] && model_id="$MODEL_ID_ARG"

  if [[ -z "$base_url" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "请输入 OpenClaw 模型 Base URL [默认 https://api.deepseek.com/v1]: " base_url
      base_url="${base_url:-https://api.deepseek.com/v1}"
    else
      die "未提供 OPENCLAW_MODEL_BASE_URL，请使用 --model-base-url=..."
    fi
  fi

  if [[ -z "$api_key" ]]; then
    if [[ -t 0 ]]; then
      read -r -s -p "请输入 OpenClaw 模型 API Key: " api_key
      echo
    else
      die "未提供 OPENCLAW_MODEL_API_KEY，请使用 --api-key=..."
    fi
  fi

  [[ -n "$api_key" ]] || die "API Key 不能为空"
  model_id="${model_id:-deepseek-chat}"
  token="${token:-$(openssl rand -hex 12)}"

  env_set OPENCLAW_MODEL_BASE_URL "$base_url"
  env_set OPENCLAW_MODEL_API_KEY "$api_key"
  env_set OPENCLAW_MODEL_ID "$model_id"
  env_set OPENCLAW_GATEWAY_TOKEN "$token"

  # 用户名无关路径：首次运行自动写入当前用户实际值。
  # 兼容历史模板中的 /home/pi 默认值：非 pi 用户首次运行时自动迁移。
  local current_nas_root current_voice_sdk_base
  current_nas_root="$(env_get NAS_ROOT)"
  if [[ -z "$current_nas_root" || ( "$current_nas_root" == "/home/pi/nas_share" && "$HOME" != "/home/pi" ) ]]; then
    env_set NAS_ROOT "$HOME/nas_share"
  fi

  # VOICE_SDK_BASE 也按当前用户 home 自动写入（兼容历史 /home/pi 默认）。
  current_voice_sdk_base="$(env_get VOICE_SDK_BASE)"
  if [[ -z "$current_voice_sdk_base" || ( "$current_voice_sdk_base" == "/home/pi/voice" && "$HOME" != "/home/pi" ) ]]; then
    env_set VOICE_SDK_BASE "$HOME/voice"
  fi

  env_set APP_DIR "$APP_DIR"
  env_set NAS_PUID "$(id -u)"
  env_set NAS_PGID "$(id -g)"

  log "已写入 .env:"
  log "  OPENCLAW_MODEL_BASE_URL=$base_url"
  log "  OPENCLAW_MODEL_API_KEY=$(mask_key "$api_key")"
  log "  OPENCLAW_MODEL_ID=$model_id"
  log "  NAS_ROOT=$(env_get NAS_ROOT)"
  log "  APP_DIR=$APP_DIR"
  log "  NAS_PUID/NAS_PGID=$(env_get NAS_PUID)/$(env_get NAS_PGID)"
}

wait_openclaw() {
  log "等待 OpenClaw 就绪..."
  for i in $(seq 1 120); do
    if docker_cmd exec openclaw node dist/index.js status >/dev/null 2>&1; then
      log "OpenClaw 已就绪"
      return 0
    fi
    sleep 2
  done
  die "OpenClaw 长时间未就绪，请执行: ./oc.sh logs"
}

sync_immich_key_to_env_if_exists() {
  local key_line
  key_line="$(grep -E '^IMMICH_API_KEY=' /DATA/AppData/openclaw/.env 2>/dev/null | tail -1 || true)"
  if [[ -n "$key_line" ]]; then
    env_set IMMICH_API_KEY "${key_line#IMMICH_API_KEY=}"
  fi
}

deploy_all() {
  log "开始执行一键安装流程..."

  # 1) 优先走 compose，全流程仍由 install.sh 统一调度
  if docker_cmd compose version >/dev/null 2>&1; then
    if container_exists openclaw; then
      warn "检测到已存在 openclaw，跳过重建 openclaw，尝试拉起其余核心服务"
      docker_cmd compose up -d --build \
        media_downloader knowledge_base immich-postgres immich-redis \
        immich-server immich-machine-learning jellyfin || warn "compose 拉起其余服务失败，可稍后重试"
    else
      log "检测到 docker compose，执行全家桶部署"
      docker_cmd compose up -d --build
    fi
  else
    # 2) compose 不可用时，走精简兜底路径（仅依赖现有 oc.sh 和初始化脚本）
    warn "未检测到 docker compose，切换到精简兜底安装路径"
    bash "$APP_DIR/oc.sh" deploy
  fi

  wait_openclaw

  # 直接配置模型（避免不同部署模式下环境占位差异）
  local base_url api_key model_id provider_json
  base_url="$(env_get OPENCLAW_MODEL_BASE_URL)"
  api_key="$(env_get OPENCLAW_MODEL_API_KEY)"
  model_id="$(env_get OPENCLAW_MODEL_ID)"

  provider_json=$(cat <<EOF
{
  "baseUrl": "${base_url}",
  "apiKey": "${api_key}",
  "api": "openai-completions",
  "models": [
    {
      "id": "${model_id}",
      "name": "${model_id}",
      "input": ["text", "image"],
      "contextWindow": 64000,
      "maxTokens": 8192
    }
  ]
}
EOF
)

  docker_cmd exec openclaw node dist/index.js config set models.providers.custom "$provider_json" --strict-json --merge
  docker_cmd exec openclaw node dist/index.js config set agents.defaults.model.primary "custom/${model_id}"
  docker_cmd exec openclaw node dist/index.js config set gateway.controlUi.allowedOrigins '["*"]' --strict-json || true

  # MCP + 相关能力
  bash "$APP_DIR/oc.sh" tools-nas-setup || warn "tools-nas-setup 失败"
  bash "$APP_DIR/oc.sh" tools-media-setup || warn "tools-media-setup 失败"
  bash "$APP_DIR/oc.sh" tools-kb-setup || warn "tools-kb-setup 失败"

  # Immich MCP：key 已存在（.env 或 /DATA/AppData/openclaw/.env）则由 oc.sh 注册
  sync_immich_key_to_env_if_exists
  if [[ -n "$(env_get IMMICH_API_KEY)" ]]; then
    bash "$APP_DIR/oc.sh" tools-immich-setup || warn "tools-immich-setup 失败，可稍后手动运行 ./oc.sh tools-immich-setup"
  else
    warn "未检测到 IMMICH_API_KEY，跳过 immich MCP。可稍后在 Immich 生成 key 后运行 ./oc.sh tools-immich-setup"
  fi

  # CasaOS 附加组件（失败不阻断主流程）
  # jellyfin 在 compose 模式下通常已存在，避免重复安装冲突
  if ! container_exists jellyfin; then
    bash "$APP_DIR/oc.sh" jellyfin-deploy || warn "jellyfin-deploy 失败"
  fi
  if ! container_exists filebrowser; then
    bash "$APP_DIR/oc.sh" nas-files-deploy || warn "nas-files-deploy 失败"
  fi
  if ! container_exists voice_assistant; then
    bash "$APP_DIR/oc.sh" voice-assistant-deploy || warn "voice-assistant-deploy 失败"
  fi

  docker_cmd restart openclaw >/dev/null 2>&1 || true
}

summary() {
  local ip token
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  token="$(env_get OPENCLAW_GATEWAY_TOKEN)"
  echo
  echo "============================================================"
  echo "安装完成"
  echo "OpenClaw : https://${ip:-<IP>}:24190/#token=${token:-casaos}"
  echo "Immich   : http://${ip:-<IP>}:2283"
  echo "Jellyfin : http://${ip:-<IP>}:8096"
  echo "文件浏览 : http://${ip:-<IP>}:28085"
  echo "对话助手 : http://${ip:-<IP>}:28083"
  echo "============================================================"
  echo "常用维护命令:"
  echo "  ./oc.sh status"
  echo "  ./oc.sh logs"
  echo "  ./oc.sh tools-sync"
}

# =============================================================================
# firewall — 幂等放行 NAS-Demo 所需 TCP 端口并持久化到 /etc/iptables/rules.v4
#
# 说明：
#   - 宿主机 INPUT 链默认 DROP（仅 22/3389/5900 等白名单放行），
#     本机服务（CasaOS 80、Immich 2283、Jellyfin 8096、OpenClaw 24190、
#     filebrowser 28085、对话助手 28083 等）需要显式放行才能局域网访问。
#   - 幂等：已存在的规则自动跳过，重复执行安全。
#   - 持久化：先备份 /etc/iptables/rules.v4 再写入，重启后仍生效。
#   - 需要 root；本机 sudo 可免密执行 docker，因此优先用 docker+nsenter
#     进入宿主机网络命名空间执行 iptables，避免 sudo 密码交互。
# =============================================================================
_ipt_in_host() {
  # 在宿主机 network namespace 执行 iptables（通过 privileged 容器 nsenter）
  docker run --rm --privileged --net=host --pid=host \
    alpine:3.20 sh -lc \
    'apk add --no-cache iptables >/dev/null 2>&1; nsenter -t 1 -n iptables "$@"' \
    _ "$@"
}

_rule_exists() {
  # iptables -C 存在则返回 0；不存在返回 1（规则不存在不是错误）
  local port="$1"
  if _ipt_in_host -C INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

cmd_firewall() {
  local changed=0 p
  log "放行 NAS-Demo 所需 TCP 端口：$FIREWALL_PORTS"
  for p in $FIREWALL_PORTS; do
    if _rule_exists "$p"; then
      log "  端口 $p 已放行，跳过"
    else
      _ipt_in_host -A INPUT -p tcp --dport "$p" -j ACCEPT
      log "  端口 $p 已放行"
      changed=1
    fi
  done
  if [[ "$changed" -eq 1 ]]; then
    log "持久化规则到 /etc/iptables/rules.v4（先备份）..."
    docker run --rm --privileged --net=host --pid=host \
      -v /etc/iptables:/etc/iptables \
      alpine:3.20 sh -lc '
        apk add --no-cache iptables >/dev/null 2>&1
        ts=$(date +%Y%m%d_%H%M%S)
        cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak.${ts} 2>/dev/null || true
        nsenter -t 1 -n iptables-save > /etc/iptables/rules.v4
        echo "已写入 rules.v4（备份 rules.v4.bak.${ts}）"
      ' || warn "持久化失败，规则仅当前生效；请手动执行 sudo sh -c 'iptables-save > /etc/iptables/rules.v4'"
  else
    log "无新增规则，跳过持久化"
  fi
  log "防火墙放行完成"
}

# =============================================================================
# reset — 把 OpenClaw 配置重置为 bootstrap 模板并重启容器
# =============================================================================
cmd_reset() {
  local bootstrap="$APP_DIR/openclaw.bootstrap.json"
  local data_dir="/DATA/AppData/openclaw"
  local target="$data_dir/openclaw.json"

  [[ -f "$bootstrap" ]] || die "缺少 bootstrap 配置: $bootstrap"
  docker_cmd ps -a --format '{{.Names}}' | grep -qx openclaw || die "未找到 openclaw 容器"

  log "重置 OpenClaw 配置为 bootstrap 模板..."
  if [[ "$EUID" -eq 0 ]]; then
    mkdir -p "$data_dir"
    cp "$bootstrap" "$target"
  else
    sudo mkdir -p "$data_dir"
    sudo cp "$bootstrap" "$target"
  fi
  log "已重置: $target"
  docker_cmd restart openclaw
  log "openclaw 已重启，可用 ./oc.sh deploy 重新按需部署"
}

# =============================================================================
# bootstrap — 若当前目录还没有 NAS-Demo 代码，自动 clone 后重新执行 install.sh
# =============================================================================
bootstrap_if_needed() {
  if [[ -f "$APP_DIR/oc.sh" && -f "$APP_DIR/docker-compose.yml" ]]; then
    return 0
  fi
  log "当前目录不是 NAS-Demo 仓库，自动拉取代码: $INSTALL_REPO"
  require_cmd git
  local target="${PWD}/NAS-Demo"
  if [[ -d "$target/.git" ]]; then
    log "已存在 $target，直接更新"
    git -C "$target" pull --ff-only || warn "git pull 失败，使用现有代码继续"
  else
    git clone "$INSTALL_REPO" "$target" || die "git clone 失败，请检查仓库地址/网络，或先手动 clone 后进入目录运行"
  fi
  exec bash "$target/install.sh" "$@"
}

main() {
  bootstrap_if_needed "$@"
  if [[ "$RESET" -eq 1 ]]; then
    cmd_reset
    exit 0
  fi
  check_env
  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    log "--check 完成"
    exit 0
  fi
  if [[ "$ARG_FIREWALL_ONLY" -eq 1 ]]; then
    cmd_firewall
    exit 0
  fi
  prepare_env
  if [[ "$SKIP_FIREWALL" -eq 0 ]]; then
    cmd_firewall || warn "防火墙放行失败，可稍后手动执行: bash install.sh firewall"
  else
    log "已按 --skip-firewall 跳过防火墙放行"
  fi
  deploy_all
  summary
}

main "$@"
