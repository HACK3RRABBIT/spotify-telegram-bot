#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/YOUR_USERNAME/spotify-telegram-bot"
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

# --- Conda check ---
if ! command -v conda &>/dev/null; then
    warn "Conda is not installed system-wide."
    warn "Please install Miniconda first: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi

CONDA_BASE=$(conda info --base)

# --- Spotdl conda environment ---
if conda env list | grep -q "^$CONDA_ENV "; then
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
    info "Cloning project to $INSTALL_DIR..."
    git clone "$REPO" "$INSTALL_DIR" 2>/dev/null || {
        warn "Git clone failed. Copying local files instead..."
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        cp -r "$SCRIPT_DIR" "$INSTALL_DIR"
    }
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
