#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="$ROOT_DIR/state/telegram-bot.log"
mkdir -p "$ROOT_DIR/state"

if pgrep -f 'python -m app.telegram_bot' >/dev/null 2>&1; then
  echo "Telegram bot is already running."
  exit 0
fi

setsid nohup "$ROOT_DIR/scripts/run_bot.sh" >>"$LOG_PATH" 2>&1 </dev/null &
echo "Started Telegram bot in background. Log: $LOG_PATH"
