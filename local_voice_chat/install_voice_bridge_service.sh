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

PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
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

echo "voice-bridge service installed and started"
sudo systemctl status --no-pager --lines=20 voice-bridge || true
