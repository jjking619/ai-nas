#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_SRC="$SCRIPT_DIR/voice-bridge.service"
SERVICE_DST="/etc/systemd/system/voice-bridge.service"

if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  source "$APP_DIR/.env"
  set +a
fi

if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  RUN_USER="$SUDO_USER"
else
  RUN_USER="$(id -un)"
fi

# ---------------------------------------------------------------------------
# 系统级：USB 麦克风访问（任何用户部署时自动生效）
# - /dev/snd 设备属 root:audio，服务进程用户需在 audio 组才能 ALSA 直连录音。
# - 高通定制 system pulse(PAL) 不暴露 USB 声卡，故 voice-bridge 默认走 ALSA；
#   麦克风用卡名 plughw:Audio,0 引用（与 USB 插拔顺序无关）。
# ---------------------------------------------------------------------------
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx audio; then
  if sudo usermod -aG audio "$RUN_USER" 2>/dev/null; then
    echo "[install] $RUN_USER 已加入 audio 组（可访问 USB 麦克风）"
  else
    echo "[install][WARN] 无法将 $RUN_USER 加入 audio 组，USB 麦克风录音可能不可用" >&2
  fi
fi

env_ensure() {
  local key="$1" value="$2"
  local cur=""
  if grep -qE "^[[:space:]]*${key}=" "$APP_DIR/.env" 2>/dev/null; then
    cur="$(grep -E "^[[:space:]]*${key}=" "$APP_DIR/.env" | tail -1 | cut -d= -f2-)"
    # 用户显式配置过（非默认占位值）则尊重用户，不覆盖
    if [[ -n "$cur" && "$cur" != "auto" && "$cur" != "default" ]]; then
      return 0
    fi
    sed -i "s#^[[:space:]]*${key}=.*#${key}=${value}#" "$APP_DIR/.env"
  else
    printf '%s=%s\n' "$key" "$value" >> "$APP_DIR/.env"
  fi
}
# 仅当未配置或仍为默认占位时写入 USB 直连配置
env_ensure VOICE_RECORD_BACKEND "alsa"
env_ensure VOICE_MIC_INPUT "plughw:Audio,0"

RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

# 优先选择装有 numpy/sherpa_onnx 的 python（pyenv 或用户 PATH），
# 避免 sudo 环境下探测到系统 /usr/bin/python3（无依赖导致服务启动崩溃）。
# 注意：依赖装在 $RUN_USER_HOME/.local（用户级 pip），sudo 下 HOME=/root 会找不到，
# 因此探测时通过 -c 直接注入 PYTHONPATH=~/.local/lib/python3.x/site-packages 兜底。
detect_python() {
  local cand py user_site
  cand="$(command -v python3 || true)"
  user_site="$(find "$RUN_USER_HOME/.local/lib" -maxdepth 2 -name site-packages -type d 2>/dev/null | head -1)"
  for py in "$cand" "$RUN_USER_HOME/.pyenv/versions/"*/bin/python3 \
            /usr/bin/python3 /usr/local/bin/python3; do
    [[ -n "$py" && -x "$py" ]] || continue
    if [[ -n "$user_site" ]]; then
      if PYTHONPATH="$user_site" "$py" -c "import numpy, sherpa_onnx" >/dev/null 2>&1; then
        echo "$py"
        return 0
      fi
    elif "$py" -c "import numpy, sherpa_onnx" >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  return 1
}
PYTHON_BIN="$(detect_python)" || { echo "[install][ERROR] 找不到装有 numpy/sherpa_onnx 的 python3，请先安装依赖" >&2; exit 1; }
VOICE_SDK_BASE="${VOICE_SDK_BASE:-$RUN_USER_HOME/voice}"

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "service file not found: $SERVICE_SRC"
  exit 1
fi

run_user_escaped="$(escape_sed_replacement "$RUN_USER")"
app_dir_escaped="$(escape_sed_replacement "$APP_DIR")"
python_bin_escaped="$(escape_sed_replacement "$PYTHON_BIN")"
voice_sdk_base_escaped="$(escape_sed_replacement "$VOICE_SDK_BASE")"

tmp_service="$(mktemp /tmp/voice-bridge.service.XXXXXX)"
sed \
  -e "s#__RUN_USER__#${run_user_escaped}#g" \
  -e "s#__APP_DIR__#${app_dir_escaped}#g" \
  -e "s#__PYTHON_BIN__#${python_bin_escaped}#g" \
  -e "s#__VOICE_SDK_BASE__#${voice_sdk_base_escaped}#g" \
  "$SERVICE_SRC" > "$tmp_service"

sudo cp "$tmp_service" "$SERVICE_DST"
rm -f "$tmp_service"
sudo systemctl daemon-reload
sudo systemctl enable --now voice-bridge
# enable --now 不会重启已运行实例；显式 restart 让 audio 组与 .env 新配置立即生效
sudo systemctl restart voice-bridge

echo "voice-bridge service installed and started"
sudo systemctl status --no-pager --lines=20 voice-bridge || true
