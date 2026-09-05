#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$ROOT_DIR/state"
BOT_ENV_PATH="$STATE_DIR/bot.env"
LOG_PATH="$STATE_DIR/telegram-bot.log"
SYSTEMD_UNIT_NAME="ai-video-gen-bot.service"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SYSTEMD_UNIT_NAME}"
INSTALL_USER="${SUDO_USER:-$USER}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" 2>/dev/null | cut -d: -f6 || true)"
INSTALL_HOME="${INSTALL_HOME:-$HOME}"
PKG_MANAGER=""
OS_ID=""
OS_NAME="Linux"
SUDO=""
AUTOSTART_METHOD="disabled"

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'
  YELLOW=$'\033[33m'
  BLUE=$'\033[34m'
  RED=$'\033[31m'
  RESET=$'\033[0m'
else
  GREEN=""
  YELLOW=""
  BLUE=""
  RED=""
  RESET=""
fi

info() {
  printf '%s==>%s %s\n' "$BLUE" "$RESET" "$*"
}

success() {
  printf '%s✔%s %s\n' "$GREEN" "$RESET" "$*"
}

warn() {
  printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2
}

fail() {
  printf '%s✖%s %s\n' "$RED" "$RESET" "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ensure_repo_layout() {
  [[ -d "$ROOT_DIR/backend/app" ]] || fail "Run installer.sh from the project root."
  [[ -f "$ROOT_DIR/scripts/run_bot.sh" ]] || fail "Missing scripts/run_bot.sh"
  [[ -f "$ROOT_DIR/pyproject.toml" ]] || fail "Missing pyproject.toml"
  [[ -f "$ROOT_DIR/package.json" ]] || fail "Missing package.json"
  [[ -f "$ROOT_DIR/quotes.csv" ]] || fail "Missing quotes.csv"
  [[ -d "$ROOT_DIR/images" ]] || fail "Missing images/ directory"
  [[ -d "$ROOT_DIR/music" ]] || fail "Missing music/ directory"
  [[ -d "$ROOT_DIR/fonts" ]] || fail "Missing fonts/ directory"
  mkdir -p "$STATE_DIR"
}

load_existing_env() {
  if [[ -f "$BOT_ENV_PATH" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$BOT_ENV_PATH"
    set +a
  fi
}

choose_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    SUDO=""
  elif command_exists sudo; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

detect_os() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_ID="${ID:-linux}"
    OS_NAME="${PRETTY_NAME:-${NAME:-Linux}}"
  fi

  if command_exists apt-get; then
    PKG_MANAGER="apt"
  elif command_exists dnf; then
    PKG_MANAGER="dnf"
  elif command_exists yum; then
    PKG_MANAGER="yum"
  elif command_exists pacman; then
    PKG_MANAGER="pacman"
  elif command_exists zypper; then
    PKG_MANAGER="zypper"
  elif command_exists apk; then
    PKG_MANAGER="apk"
  else
    PKG_MANAGER=""
  fi
}

mask_value() {
  local value="$1"
  if [[ -z "$value" ]]; then
    printf 'not set'
    return
  fi
  if [[ "${#value}" -le 8 ]]; then
    printf '********'
    return
  fi
  printf '%s***%s' "${value:0:4}" "${value: -4}"
}

prompt_text() {
  local prompt="$1"
  local default_value="${2:-}"
  local reply=""
  if [[ -n "$default_value" ]]; then
    read -r -p "$prompt [$default_value]: " reply
    printf '%s' "${reply:-$default_value}"
  else
    read -r -p "$prompt: " reply
    printf '%s' "$reply"
  fi
}

prompt_secret() {
  local prompt="$1"
  local default_value="${2:-}"
  local reply=""
  if [[ -n "$default_value" ]]; then
    printf '%s [%s]: ' "$prompt" "$(mask_value "$default_value")"
  else
    printf '%s: ' "$prompt"
  fi
  read -r -s reply
  printf '\n'
  printf '%s' "${reply:-$default_value}"
}

prompt_yes_no() {
  local prompt="$1"
  local default_answer="$2"
  local suffix="[y/N]"
  local fallback="n"
  local reply=""
  if [[ "$default_answer" == "y" ]]; then
    suffix="[Y/n]"
    fallback="y"
  fi
  while true; do
    read -r -p "$prompt $suffix " reply
    reply="${reply:-$fallback}"
    case "${reply,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
    warn "Please answer y or n."
  done
}

validate_chat_ids() {
  local ids="${1// /}"
  [[ "$ids" =~ ^[0-9]+(,[0-9]+)*$ ]]
}

contains_chat_id() {
  local ids=",$1,"
  local wanted=",$2,"
  [[ "$ids" == *"$wanted"* ]]
}

validate_bot_token() {
  local token="$1"
  python3 - "$token" <<'PY'
import json
import sys
import urllib.error
import urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/getMe"
try:
    with urllib.request.urlopen(url, timeout=15) as response:
        data = json.load(response)
except Exception as exc:
    print(f"network:{exc}", file=sys.stderr)
    raise SystemExit(2)
if not data.get("ok"):
    print(data, file=sys.stderr)
    raise SystemExit(1)
print(data["result"].get("username", "bot"))
PY
}

validate_default_chat() {
  local token="$1"
  local chat_id="$2"
  python3 - "$token" "$chat_id" <<'PY'
import json
import sys
import urllib.error
import urllib.request

token, chat_id = sys.argv[1], sys.argv[2]
url = f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}"
try:
    with urllib.request.urlopen(url, timeout=15) as response:
        data = json.load(response)
except Exception as exc:
    print(f"network:{exc}", file=sys.stderr)
    raise SystemExit(2)
if not data.get("ok"):
    print(data, file=sys.stderr)
    raise SystemExit(1)
print(data["result"].get("title") or data["result"].get("username") or data["result"].get("first_name") or "chat")
PY
}

collect_configuration() {
  local existing_token="${AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN:-}"
  local existing_allowed="${AI_VIDEO_GEN_ALLOWED_CHAT_IDS:-1702319284}"
  local default_chat_existing="${AI_VIDEO_GEN_DEFAULT_CHAT_ID:-}"
  local default_chat_guess="${default_chat_existing:-${existing_allowed%%,*}}"

  info "Collecting Telegram bot configuration"
  while true; do
    AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN="$(prompt_secret "Telegram bot token" "$existing_token")"
    [[ -n "$AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN" ]] || { warn "Bot token is required."; continue; }
    break
  done

  while true; do
    AI_VIDEO_GEN_ALLOWED_CHAT_IDS="$(prompt_text "Allowed chat ID(s), comma-separated" "$existing_allowed")"
    AI_VIDEO_GEN_ALLOWED_CHAT_IDS="${AI_VIDEO_GEN_ALLOWED_CHAT_IDS// /}"
    validate_chat_ids "$AI_VIDEO_GEN_ALLOWED_CHAT_IDS" && break
    warn "Enter numeric chat IDs like 1702319284 or 1702319284,123456789."
  done

  while true; do
    AI_VIDEO_GEN_DEFAULT_CHAT_ID="$(prompt_text "Default chat ID" "$default_chat_guess")"
    [[ "$AI_VIDEO_GEN_DEFAULT_CHAT_ID" =~ ^[0-9]+$ ]] || { warn "Default chat ID must be numeric."; continue; }
    contains_chat_id "$AI_VIDEO_GEN_ALLOWED_CHAT_IDS" "$AI_VIDEO_GEN_DEFAULT_CHAT_ID" && break
    warn "Default chat ID must be included in allowed chat IDs."
  done

  if prompt_yes_no "Enable auto start when the system boots?" "y"; then
    ENABLE_AUTOSTART="y"
  else
    ENABLE_AUTOSTART="n"
  fi
  if prompt_yes_no "Start the bot after installation?" "y"; then
    START_BOT_NOW="y"
  else
    START_BOT_NOW="n"
  fi
}

ensure_package_manager() {
  [[ -n "$PKG_MANAGER" ]] || fail "Unsupported Linux package manager. Install Python 3.11+, pip, venv, FFmpeg, Node.js, npm, and screen manually."
}

run_pkg_install() {
  local -a packages=("$@")
  [[ "${#packages[@]}" -gt 0 ]] || return 0
  if [[ -z "$SUDO" && "${EUID}" -ne 0 ]]; then
    fail "Need root or sudo to install system packages: ${packages[*]}"
  fi
  case "$PKG_MANAGER" in
    apt)
      $SUDO apt-get update
      DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "${packages[@]}"
      ;;
    dnf)
      $SUDO dnf install -y "${packages[@]}"
      ;;
    yum)
      $SUDO yum install -y "${packages[@]}"
      ;;
    pacman)
      $SUDO pacman -Sy --noconfirm "${packages[@]}"
      ;;
    zypper)
      $SUDO zypper --non-interactive install "${packages[@]}"
      ;;
    apk)
      $SUDO apk add --no-cache "${packages[@]}"
      ;;
    *)
      fail "Unsupported package manager: $PKG_MANAGER"
      ;;
  esac
}

install_system_packages() {
  ensure_package_manager
  local -a packages=()
  case "$PKG_MANAGER" in
    apt)
      packages=(python3 python3-pip python3-venv ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen cron)
      ;;
    dnf)
      packages=(python3 python3-pip ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen cronie)
      ;;
    yum)
      packages=(python3 python3-pip ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen cronie)
      ;;
    pacman)
      packages=(python python-pip ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen cronie)
      ;;
    zypper)
      packages=(python3 python3-pip python3-virtualenv ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen cron)
      ;;
    apk)
      packages=(python3 py3-pip ffmpeg nodejs npm)
      [[ "$ENABLE_AUTOSTART" == "y" ]] && packages+=(screen dcron)
      ;;
  esac
  info "Installing required system packages with $PKG_MANAGER on $OS_NAME"
  run_pkg_install "${packages[@]}"
}

ensure_python_venv() {
  info "Preparing Python virtual environment"
  if [[ ! -d "$ROOT_DIR/.venv" ]]; then
    python3 -m venv "$ROOT_DIR/.venv"
  fi
  "$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  (cd "$ROOT_DIR" && "$ROOT_DIR/.venv/bin/pip" install -e '.[dev]')
}

ensure_node_modules() {
  info "Installing Node dependencies"
  (cd "$ROOT_DIR" && npm install)
}

validate_runtime() {
  info "Validating installed runtime"
  ffmpeg -version >/dev/null
  "$ROOT_DIR/.venv/bin/python" -m py_compile "$ROOT_DIR"/backend/app/*.py
  (cd "$ROOT_DIR" && node -c upload.js >/dev/null)
}

write_bot_env() {
  info "Writing persistent bot configuration"
  mkdir -p "$STATE_DIR"
  local tmp_path="${BOT_ENV_PATH}.tmp"
  cat >"$tmp_path" <<EOF
AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN="$AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN"
AI_VIDEO_GEN_ALLOWED_CHAT_IDS="$AI_VIDEO_GEN_ALLOWED_CHAT_IDS"
AI_VIDEO_GEN_DEFAULT_CHAT_ID="$AI_VIDEO_GEN_DEFAULT_CHAT_ID"
AI_VIDEO_GEN_TELEGRAM_PARSE_MODE="${AI_VIDEO_GEN_TELEGRAM_PARSE_MODE:-HTML}"
AI_VIDEO_GEN_SEND_RETRIES="${AI_VIDEO_GEN_SEND_RETRIES:-3}"
AI_VIDEO_GEN_LOOP_BACKOFF_SECONDS="${AI_VIDEO_GEN_LOOP_BACKOFF_SECONDS:-10}"
YOUTUBE_PRIVACY_STATUS="${YOUTUBE_PRIVACY_STATUS:-public}"
YOUTUBE_CATEGORY_ID="${YOUTUBE_CATEGORY_ID:-22}"
AI_VIDEO_GEN_YOUTUBE_RETRY_LIMIT="${AI_VIDEO_GEN_YOUTUBE_RETRY_LIMIT:-5}"
EOF
  mv "$tmp_path" "$BOT_ENV_PATH"
  chmod 600 "$BOT_ENV_PATH"
}

run_as_install_user() {
  local cmd="$1"
  if [[ "${USER}" == "$INSTALL_USER" && "${EUID}" -ne 0 ]]; then
    bash -lc "$cmd"
  elif [[ -n "$SUDO" ]]; then
    $SUDO -u "$INSTALL_USER" bash -lc "$cmd"
  else
    su - "$INSTALL_USER" -c "$cmd"
  fi
}

has_live_systemd() {
  command_exists systemctl && [[ -d /run/systemd/system ]]
}

ensure_managed_block() {
  local file_path="$1"
  local begin_marker="$2"
  local end_marker="$3"
  local block_content="$4"
  BLOCK_CONTENT="$block_content" python3 - "$file_path" "$begin_marker" "$end_marker" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
content = os.environ["BLOCK_CONTENT"].rstrip("\n")
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()
out = []
inside = False
for line in lines:
    if line.strip() == begin:
        inside = True
        continue
    if line.strip() == end:
        inside = False
        continue
    if not inside:
        out.append(line)
while out and out[-1] == "":
    out.pop()
if content:
    out.append(begin)
    out.extend(content.splitlines())
    out.append(end)
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
}

ensure_cron_service() {
  if command_exists rc-service && command_exists rc-update; then
    $SUDO rc-update add crond default >/dev/null 2>&1 || true
    $SUDO rc-service crond start >/dev/null 2>&1 || true
    return
  fi
  if command_exists service; then
    $SUDO service cron start >/dev/null 2>&1 || $SUDO service crond start >/dev/null 2>&1 || true
    return
  fi
  if has_live_systemd; then
    $SUDO systemctl enable --now cron >/dev/null 2>&1 || $SUDO systemctl enable --now crond >/dev/null 2>&1 || true
  fi
}

configure_systemd() {
  [[ -n "$SUDO" || "${EUID}" -eq 0 ]] || return 1
  has_live_systemd || return 1
  info "Configuring systemd autostart"
  local tmp_unit="$STATE_DIR/${SYSTEMD_UNIT_NAME}"
  cat >"$tmp_unit" <<EOF
[Unit]
Description=AI Motivational Video Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$INSTALL_USER
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$BOT_ENV_PATH
ExecStart=$ROOT_DIR/scripts/run_bot.sh
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
  $SUDO cp "$tmp_unit" "$SYSTEMD_UNIT_PATH"
  $SUDO systemctl daemon-reload
  if [[ "$START_BOT_NOW" == "y" ]]; then
    $SUDO systemctl enable --now "$SYSTEMD_UNIT_NAME"
  else
    $SUDO systemctl enable "$SYSTEMD_UNIT_NAME"
  fi
  AUTOSTART_METHOD="systemd"
  return 0
}

configure_crontab() {
  command_exists crontab || return 1
  info "Configuring crontab @reboot autostart"
  local begin="# BEGIN AI_VIDEO_GEN_BOT"
  local end="# END AI_VIDEO_GEN_BOT"
  local line="@reboot cd \"$ROOT_DIR\" && \"$ROOT_DIR/scripts/start_bot_background.sh\""
  local existing=""
  if existing="$(run_as_install_user "crontab -l 2>/dev/null" || true)"; then
    :
  fi
  local temp_file
  temp_file="$(mktemp)"
  printf '%s\n' "$existing" >"$temp_file"
  ensure_managed_block "$temp_file" "$begin" "$end" "$line"
  run_as_install_user "crontab '$temp_file'"
  rm -f "$temp_file"
  ensure_cron_service
  AUTOSTART_METHOD="crontab"
  return 0
}

ensure_screen_launcher() {
  chmod +x "$ROOT_DIR/scripts/start_bot_screen.sh"
}

configure_screen_rc_local() {
  command_exists screen || return 1
  [[ -n "$SUDO" || "${EUID}" -eq 0 ]] || return 1
  info "Configuring rc.local screen fallback autostart"
  ensure_screen_launcher
  local temp_rc
  temp_rc="$(mktemp)"
  if [[ -f /etc/rc.local ]]; then
    $SUDO cp /etc/rc.local "$temp_rc"
  else
    cat >"$temp_rc" <<'EOF'
#!/bin/sh -e

exit 0
EOF
  fi
  local begin="# BEGIN AI_VIDEO_GEN_BOT"
  local end="# END AI_VIDEO_GEN_BOT"
  local command_line="su - $INSTALL_USER -c 'cd \"$ROOT_DIR\" && \"$ROOT_DIR/scripts/start_bot_screen.sh\"' >/dev/null 2>&1 || true"
  ensure_managed_block "$temp_rc" "$begin" "$end" "$command_line"
  python3 - "$temp_rc" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
filtered = []
for line in lines:
    if line.strip() == "exit 0":
        continue
    filtered.append(line)
while filtered and filtered[-1] == "":
    filtered.pop()
filtered.extend(["", "exit 0"])
path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
PY
  $SUDO cp "$temp_rc" /etc/rc.local
  $SUDO chmod +x /etc/rc.local
  rm -f "$temp_rc"
  AUTOSTART_METHOD="screen+rc.local"
  return 0
}

configure_autostart() {
  if [[ "$ENABLE_AUTOSTART" != "y" ]]; then
    AUTOSTART_METHOD="disabled"
    return 0
  fi

  if configure_systemd; then
    return 0
  fi
  if configure_crontab; then
    return 0
  fi
  if configure_screen_rc_local; then
    return 0
  fi

  warn "Automatic boot setup was not possible on this system."
  AUTOSTART_METHOD="nohup-manual"
  return 0
}

validate_telegram_access() {
  info "Validating Telegram bot token"
  local bot_name
  if bot_name="$(validate_bot_token "$AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN" 2>/tmp/ai-video-gen-token.err)"; then
    success "Bot token is valid for @$bot_name"
  else
    local code=$?
    local err
    err="$(cat /tmp/ai-video-gen-token.err 2>/dev/null || true)"
    rm -f /tmp/ai-video-gen-token.err
    if [[ $code -eq 2 ]]; then
      warn "Could not verify bot token because Telegram API was unreachable: $err"
    else
      fail "Bot token validation failed. Check the token and try again."
    fi
  fi

  info "Checking default chat visibility"
  local chat_name
  if chat_name="$(validate_default_chat "$AI_VIDEO_GEN_TELEGRAM_BOT_TOKEN" "$AI_VIDEO_GEN_DEFAULT_CHAT_ID" 2>/tmp/ai-video-gen-chat.err)"; then
    success "Default chat is reachable: $chat_name"
  else
    local code=$?
    local err
    err="$(cat /tmp/ai-video-gen-chat.err 2>/dev/null || true)"
    rm -f /tmp/ai-video-gen-chat.err
    if [[ $code -eq 2 ]]; then
      warn "Could not verify default chat because Telegram API was unreachable: $err"
    else
      warn "Telegram could not verify chat ID $AI_VIDEO_GEN_DEFAULT_CHAT_ID yet. Start the bot in Telegram once if needed."
    fi
  fi
}

bot_running() {
  pgrep -f 'python -m app.telegram_bot' >/dev/null 2>&1
}

start_bot_if_requested() {
  [[ "$START_BOT_NOW" == "y" ]] || return 0
  if bot_running; then
    success "Bot is already running."
    return 0
  fi
  info "Starting the bot"
  case "$AUTOSTART_METHOD" in
    systemd)
      $SUDO systemctl start "$SYSTEMD_UNIT_NAME"
      ;;
    screen+rc.local)
      "$ROOT_DIR/scripts/start_bot_screen.sh"
      ;;
    *)
      "$ROOT_DIR/scripts/start_bot_background.sh"
      ;;
  esac
}

print_summary() {
  printf '\n'
  success "Installation complete"
  printf 'OS: %s\n' "$OS_NAME"
  printf 'Package manager: %s\n' "${PKG_MANAGER:-manual}"
  printf 'Config: %s\n' "$BOT_ENV_PATH"
  printf 'Autostart: %s\n' "$AUTOSTART_METHOD"
  printf 'Log: %s\n' "$LOG_PATH"
  if bot_running; then
    printf 'Bot status: running\n'
  else
    printf 'Bot status: not running\n'
  fi
  printf '\n'
  printf 'Useful commands:\n'
  printf '  Start: %s/scripts/run_bot.sh\n' "$ROOT_DIR"
  printf '  Background start: %s/scripts/start_bot_background.sh\n' "$ROOT_DIR"
  printf '  Screen start: %s/scripts/start_bot_screen.sh\n' "$ROOT_DIR"
  printf '  Logs: tail -f %s\n' "$LOG_PATH"
  printf '  Edit config: ${EDITOR:-nano} %s\n' "$BOT_ENV_PATH"
}

main() {
  cd "$ROOT_DIR"
  ensure_repo_layout
  choose_sudo
  detect_os
  load_existing_env

  printf '%sAI Motivational Video Creator Installer%s\n' "$GREEN" "$RESET"
  printf 'Repo: %s\n' "$ROOT_DIR"
  printf 'Detected OS: %s\n' "$OS_NAME"
  printf '\n'

  collect_configuration
  install_system_packages
  ensure_python_venv
  ensure_node_modules
  write_bot_env
  validate_runtime
  validate_telegram_access
  configure_autostart
  start_bot_if_requested
  print_summary
}

main "$@"
