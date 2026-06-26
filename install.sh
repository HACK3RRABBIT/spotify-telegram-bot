#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/HACK3RRABBIT/spotify-telegram-bot"
INSTALL_DIR="/opt/spotify-telegram-bot"
CONDA_ENV="spotify-downloader"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    warn "This script should be run as root (sudo)."
    exit 1
fi

info "=== Spotify Telegram Bot Installer ==="

# --- System dependencies ---
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq ffmpeg git curl

# --- Find conda ---
CONDA_BASE=""
for candidate in /home/*/miniconda3 /home/*/anaconda3 /root/miniconda3 /root/anaconda3 /opt/conda; do
    if [[ -x "$candidate/bin/conda" ]]; then
        CONDA_BASE="$candidate"
        break
    fi
done

if [[ -z "$CONDA_BASE" ]]; then
    if command -v conda &>/dev/null; then
        CONDA_BASE=$(conda info --base 2>/dev/null || echo "")
    fi
fi

if [[ -z "$CONDA_BASE" || ! -x "$CONDA_BASE/bin/conda" ]]; then
    warn "Conda not found. Installing Miniconda..."
    MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
    curl -fsSL "$MINICONDA_URL" -o /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p /opt/miniconda3
    rm /tmp/miniconda.sh
    CONDA_BASE="/opt/miniconda3"
    "$CONDA_BASE/bin/conda" init
fi

info "Using Conda at: $CONDA_BASE"
export PATH="$CONDA_BASE/bin:$PATH"

# --- Spotdl conda environment ---
if "$CONDA_BASE/bin/conda" env list | grep -q "^$CONDA_ENV "; then
    info "Conda environment '$CONDA_ENV' already exists."
else
    info "Creating conda environment '$CONDA_ENV' with Python 3.11..."
    "$CONDA_BASE/bin/conda" create -n "$CONDA_ENV" python=3.11 -y
fi

info "Installing/updating spotdl..."
"$CONDA_BASE/envs/$CONDA_ENV/bin/pip" install --upgrade spotdl

# --- Clone / copy project ---
if [[ -d "$INSTALL_DIR" ]]; then
    warn "$INSTALL_DIR already exists. Updating..."
    cd "$INSTALL_DIR"
    if git rev-parse --git-dir &>/dev/null; then
        git pull
    fi
else
    info "Copying project to $INSTALL_DIR..."
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    cp -r "$SCRIPT_DIR" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- Python dependencies ---
info "Installing Python dependencies..."
"$CONDA_BASE/envs/$CONDA_ENV/bin/pip" install python-telegram-bot python-dotenv

# --- Token setup ---
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo ""
    warn "============================================="
    warn "  No .env file found."
    warn "  You need a Telegram bot token."
    warn "  Get one from https://t.me/botfather"
    warn "============================================="
    read -rp "Enter your TELEGRAM_BOT_TOKEN: " TOKEN_INPUT
    if [[ -n "$TOKEN_INPUT" ]]; then
        sed -i "s/TELEGRAM_BOT_TOKEN=/TELEGRAM_BOT_TOKEN=$TOKEN_INPUT/" .env
        info "Token saved to .env"
    else
        warn "No token entered. Edit .env manually later."
    fi
else
    info ".env already exists, skipping token prompt."
fi

# --- systemd service ---
PYTHON_PATH="$CONDA_BASE/envs/$CONDA_ENV/bin/python"

cat > /etc/systemd/system/spotify-telegram-bot.service <<SERVICE
[Unit]
Description=Spotify Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$PYTHON_PATH $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

info "Reloading systemd..."
systemctl daemon-reload

info "Enabling and starting service..."
systemctl enable spotify-telegram-bot.service
systemctl restart spotify-telegram-bot.service

echo ""
info "=== Installation complete ==="
info "Check status:  systemctl status spotify-telegram-bot"
info "View logs:     journalctl -u spotify-telegram-bot -f"
echo ""
