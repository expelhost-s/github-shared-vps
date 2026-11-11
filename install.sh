#!/usr/bin/env bash
# ============================================================
#  Docker VPS Bot - one-command installer
#
#  Usage (put install.sh and vps_bot.py in the same folder):
#     bash install.sh                         # asks for token + admin id
#     bash install.sh <BOT_TOKEN> <ADMIN_ID>  # fully non-interactive
#     bash install.sh <BOT_TOKEN> <ID1,ID2>   # multiple admins
#
#  Optional env vars: PREFIX, BRAND, EXPIRY_DAYS, ENFORCE_LIMITS, STORAGE_MODE
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/vpsbot"
SERVICE="vpsbot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; N='\033[0m'
info() { echo -e "${G}[+]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
die()  { echo -e "${R}[x]${N} $*" >&2; exit 1; }

# ---------- checks ----------
[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo bash install.sh"
command -v apt-get >/dev/null 2>&1 || die "This installer supports Debian/Ubuntu (apt) only."
[ -f "$SCRIPT_DIR/vps_bot.py" ] || die "vps_bot.py not found next to install.sh ($SCRIPT_DIR)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required."

# ---------- token + admin id ----------
TOKEN="${1:-${DISCORD_TOKEN:-}}"
ADMINS="${2:-${ADMIN_IDS:-}}"

if [ -z "$TOKEN" ]; then
  read -rp "Discord bot token: " TOKEN
fi
if [ -z "$ADMINS" ]; then
  read -rp "Admin Discord user ID (comma-separated for many): " ADMINS
fi
ADMINS="${ADMINS// /}"

[ -n "$TOKEN" ]  || die "Bot token cannot be empty."
[[ "$ADMINS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "Admin ID must be numbers only (e.g. 123456789012345678)."

# ---------- system packages ----------
info "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates \
  e2fsprogs util-linux xfsprogs >/dev/null

# ---------- docker ----------
if ! command -v docker >/dev/null 2>&1; then
  info "Installing Docker..."
  curl -fsSL https://get.docker.com | sh >/dev/null
else
  info "Docker already installed."
fi
systemctl enable --now docker >/dev/null 2>&1 || true

info "Pulling base images (Ubuntu 22.04 / 24.04)..."
docker pull ubuntu:22.04 >/dev/null
docker pull ubuntu:24.04 >/dev/null

# ---------- bot files ----------
info "Setting up $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR" /var/lib/vpsbot
cp "$SCRIPT_DIR/vps_bot.py" "$INSTALL_DIR/vps_bot.py"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q "discord.py>=2.3" python-dotenv

# ---------- .env ----------
info "Writing .env ..."
cat > "$INSTALL_DIR/.env" <<EOF
DISCORD_TOKEN=${TOKEN}
ADMIN_IDS=${ADMINS}
PREFIX=${PREFIX:-2}
BRAND=${BRAND:-FLOATIX}
EXPIRY_DAYS=${EXPIRY_DAYS:-30}
ENFORCE_LIMITS=${ENFORCE_LIMITS:-false}
STORAGE_MODE=${STORAGE_MODE:-auto}
DISK_DIR=/var/lib/vpsbot
DB_PATH=${INSTALL_DIR}/vpsbot.db
EOF
chmod 600 "$INSTALL_DIR/.env"

# ---------- systemd service ----------
info "Creating systemd service..."
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Docker VPS Discord Bot
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/vps_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo
  info "Bot is running!"
else
  warn "Service did not start. Check logs:  journalctl -u ${SERVICE} -n 50 --no-pager"
fi

cat <<EOF

 Done.  Useful commands:
   systemctl status ${SERVICE}          # status
   journalctl -u ${SERVICE} -f          # live logs
   systemctl restart ${SERVICE}         # restart
   nano ${INSTALL_DIR}/.env             # edit config (then restart)

 In Discord, type:  ${PREFIX:-2}help
EOF
