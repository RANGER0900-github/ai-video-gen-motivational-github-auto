#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION_NAME="ai-video-gen-bot"
LOG_PATH="$ROOT_DIR/state/telegram-bot.log"
mkdir -p "$ROOT_DIR/state"

if ! command -v screen >/dev/null 2>&1; then
  echo "screen is not installed."
  exit 1
fi

if screen -ls | grep -q "[0-9][0-9]*\\.${SESSION_NAME}[[:space:]]"; then
  echo "Telegram bot screen session is already running: $SESSION_NAME"
  exit 0
fi

screen -dmS "$SESSION_NAME" bash -lc "cd '$ROOT_DIR' && '$ROOT_DIR/scripts/run_bot.sh' >>'$LOG_PATH' 2>&1"
echo "Started Telegram bot in detached screen session: $SESSION_NAME"
