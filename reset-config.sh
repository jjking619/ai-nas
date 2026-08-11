#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/pi/openclaw-casaos"
BOOTSTRAP_CONFIG="$APP_DIR/openclaw.bootstrap.json"
DATA_DIR="/DATA/AppData/openclaw"
TARGET_CONFIG="$DATA_DIR/openclaw.json"

if [[ ! -f "$BOOTSTRAP_CONFIG" ]]; then
  echo "Bootstrap config not found: $BOOTSTRAP_CONFIG"
  exit 1
fi

sudo mkdir -p "$DATA_DIR"
sudo cp "$BOOTSTRAP_CONFIG" "$TARGET_CONFIG"
echo "Reset config to bootstrap: $TARGET_CONFIG"
echo "Now restart container with: sudo docker restart openclaw"