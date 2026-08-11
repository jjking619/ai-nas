#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/voice-bridge.service"
SERVICE_DST="/etc/systemd/system/voice-bridge.service"

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "service file not found: $SERVICE_SRC"
  exit 1
fi

sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable --now voice-bridge

echo "voice-bridge service installed and started"
sudo systemctl status --no-pager --lines=20 voice-bridge || true
