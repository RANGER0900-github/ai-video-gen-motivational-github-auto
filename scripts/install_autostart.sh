#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT_DIR/scripts/start_bot_background.sh"

if command -v systemctl >/dev/null 2>&1; then
  echo "systemd detected."
  echo "Copy deploy/ai-video-gen-bot.service to /etc/systemd/system/ and then run:"
  echo "  sudo systemctl daemon-reload"
  echo "  sudo systemctl enable --now ai-video-gen-bot.service"
  exit 0
fi

if command -v crontab >/dev/null 2>&1; then
  echo "systemd not detected. Add this line to your crontab with 'crontab -e':"
  echo "@reboot cd $ROOT_DIR && $RUNNER"
  exit 0
fi

echo "No supported service manager detected."
echo "Run this command manually after reboot/login:"
echo "  cd $ROOT_DIR && $RUNNER"
