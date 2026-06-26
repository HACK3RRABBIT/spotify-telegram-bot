#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Spotify Telegram Bot — Full Installer
#
# Installs the bot and sets up the Cloudflare WARP proxy layer that bypasses
# YouTube's server IP block. After this script finishes, the bot is fully
# operational. YouTube downloads may still require fresh cookies for some
# content — see the README for how to upload cookies.txt via Telegram.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="https://github.com/HACK3RRABBIT/spotify-telegram-bot"
INSTALL_DIR="/opt/spotify-telegram-bot"
CONDA_ENV="spotify-downloader"
WARP_SOCKS_PORT=40000
PRIVOXY_PORT=8118

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
step()  { echo -e "${BLUE}[STEP]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    warn "This script must be run as root (sudo)."
    exit 1
fi

info "=== Spotify Telegram Bot Installer ==="
echo ""

# ── 1. System dependencies ────────────────────────────────────────────────────
step "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq ffmpeg git curl gnupg lsb-release ca-certificates

# ── 2. Cloudflare WARP ────────────────────────────────────────────────────────
# WARP routes all yt-dlp / spotdl traffic through Cloudflare's edge network,
# giving the server a Cloudflare IP (AS13335). YouTube does not block Cloudflare
# IPs the same way it blocks generic datacenter/VPS IPs. Combined with the
# bgutil PO-token plugin, this allows YouTube downloads without a residential
# proxy service.
step "Setting up Cloudflare WARP proxy..."

curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor \
      --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg

echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] \
https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
    | tee /etc/apt/sources.list.d/cloudflare-client.list > /dev/null

DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflare-warp

systemctl enable warp-svc
systemctl start  warp-svc
sleep 2

# Register (free — no account needed; just accept ToS)
if ! warp-cli --accept-tos status 2>/dev/null | grep -q "Registered"; then
    warp-cli --accept-tos registration new
    sleep 1
fi

warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port $WARP_SOCKS_PORT
warp-cli --accept-tos connect
sleep 4

# Systemd unit to reconnect WARP on every boot
cat > /etc/systemd/system/warp-connect.service <<'UNIT'
[Unit]
Description=Connect Cloudflare WARP on boot
After=warp-svc.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/warp-cli --accept-tos connect
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable warp-connect

WARP_IP=$(curl -s --proxy "socks5://127.0.0.1:${WARP_SOCKS_PORT}" --max-time 8 \
    https://ipinfo.io/ip 2>/dev/null || echo "unknown")
info "WARP connected. Exit IP: $WARP_IP (Cloudflare)"

# ── 3. Privoxy (HTTP→SOCKS5 bridge for spotdl) ───────────────────────────────
# spotdl only accepts HTTP/HTTPS proxy URLs; privoxy converts HTTP to WARP SOCKS5.
step "Installing privoxy (HTTP→SOCKS5 bridge)..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq privoxy

# Add forward rule if not already present
if ! grep -q "forward-socks5 / 127.0.0.1:${WARP_SOCKS_PORT}" /etc/privoxy/config; then
    echo "" >> /etc/privoxy/config
    echo "# Forward all traffic through Cloudflare WARP SOCKS5 proxy" >> /etc/privoxy/config
    echo "forward-socks5 / 127.0.0.1:${WARP_SOCKS_PORT} ." >> /etc/privoxy/config
fi

systemctl enable privoxy
systemctl restart privoxy
info "Privoxy running — HTTP proxy at 127.0.0.1:${PRIVOXY_PORT}"

# ── 4. Conda / Python environment ─────────────────────────────────────────────
step "Setting up Python environment..."
CONDA_BASE=""
for candidate in /home/*/miniconda3 /home/*/anaconda3 /root/miniconda3 \
                 /root/anaconda3 /opt/conda /opt/miniconda3; do
    if [[ -x "$candidate/bin/conda" ]]; then
        CONDA_BASE="$candidate"
        break
    fi
done

if [[ -z "$CONDA_BASE" ]] && command -v conda &>/dev/null; then
    CONDA_BASE=$(conda info --base 2>/dev/null || echo "")
fi

if [[ -z "$CONDA_BASE" || ! -x "$CONDA_BASE/bin/conda" ]]; then
    warn "Conda not found. Installing Miniconda..."
    curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
        -o /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p /opt/miniconda3
    rm /tmp/miniconda.sh
    CONDA_BASE="/opt/miniconda3"
    "$CONDA_BASE/bin/conda" init
fi

info "Using Conda at: $CONDA_BASE"
export PATH="$CONDA_BASE/bin:$PATH"

if "$CONDA_BASE/bin/conda" env list | grep -q "^$CONDA_ENV "; then
    info "Conda environment '$CONDA_ENV' already exists."
else
    info "Creating conda environment '$CONDA_ENV' with Python 3.11..."
    "$CONDA_BASE/bin/conda" create -n "$CONDA_ENV" python=3.11 -y
fi

PIP="$CONDA_BASE/envs/$CONDA_ENV/bin/pip"

info "Installing Python packages..."
"$PIP" install --upgrade \
    python-telegram-bot \
    python-dotenv \
    spotdl \
    httpx \
    bgutil-ytdlp-pot-provider

# ── 5. bgutil PO-token provider (Node.js server) ─────────────────────────────
# bgutil generates YouTube Proof-of-Origin tokens that satisfy bot-detection
# challenges. Runs as a persistent HTTP server on port 4416.
# Combined with WARP, this lets the server pass YouTube's checks reliably.
step "Installing bgutil yt-dlp PO-token provider..."
BGUTIL_DIR="/root/bgutil-ytdlp-pot-provider"

# Install Node.js if not present
if ! command -v node &>/dev/null; then
    info "Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
fi

if [[ ! -d "$BGUTIL_DIR" ]]; then
    git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider "$BGUTIL_DIR"
else
    git -C "$BGUTIL_DIR" pull
fi

cd "$BGUTIL_DIR/server"
npm install --prefer-offline 2>/dev/null || npm install
npm run build 2>/dev/null || true
cd -

# Systemd service for bgutil server (port 4416)
NODE_BIN=$(command -v node)
cat > /etc/systemd/system/bgutil-pot-server.service <<UNIT
[Unit]
Description=bgutil YouTube PO-Token Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BGUTIL_DIR/server
ExecStart=$NODE_BIN $BGUTIL_DIR/server/build/index.js
Restart=always
RestartSec=5
Environment=PORT=4416

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable bgutil-pot-server
systemctl restart bgutil-pot-server
sleep 2

if systemctl is-active --quiet bgutil-pot-server; then
    info "bgutil PO-token server running on port 4416"
else
    warn "bgutil server failed to start — YouTube downloads may be limited"
fi

# ── 6. Clone / update bot ─────────────────────────────────────────────────────
step "Installing bot..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    warn "$INSTALL_DIR already exists. Pulling latest..."
    git -C "$INSTALL_DIR" pull
elif [[ ! -d "$INSTALL_DIR" ]]; then
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 7. .env configuration ─────────────────────────────────────────────────────
step "Configuring environment..."
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
    info ".env already exists."
fi

# Add proxy config if not already present
if ! grep -q "YTDLP_PROXY" .env; then
    {
        echo ""
        echo "# Cloudflare WARP proxy (routes yt-dlp through Cloudflare IPs)"
        echo "YTDLP_PROXY=socks5://127.0.0.1:${WARP_SOCKS_PORT}"
        echo ""
        echo "# HTTP proxy via privoxy (for spotdl, which requires HTTP proxy format)"
        echo "SPOTDL_PROXY=http://127.0.0.1:${PRIVOXY_PORT}"
    } >> .env
    info "Proxy settings added to .env"
fi

# ── 8. Systemd service ────────────────────────────────────────────────────────
step "Installing systemd service..."
PYTHON_PATH="$CONDA_BASE/envs/$CONDA_ENV/bin/python"
DENO_PATH=$(command -v deno 2>/dev/null || echo "/root/.deno/bin/deno")

cat > /etc/systemd/system/spotify-telegram-bot.service <<SERVICE
[Unit]
Description=Spotify Telegram Bot
After=network-online.target warp-svc.service warp-connect.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment="PATH=$CONDA_BASE/envs/$CONDA_ENV/bin:/root/.deno/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$PYTHON_PATH $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable spotify-telegram-bot.service
systemctl restart spotify-telegram-bot.service

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "=== Installation complete ==="
echo ""
echo "  Proxy stack:  WARP (Cloudflare exit IP) → privoxy (HTTP bridge)"
echo "  Exit IP:      $WARP_IP"
echo ""
echo "  Bot service:  systemctl status spotify-telegram-bot"
echo "  Bot logs:     journalctl -u spotify-telegram-bot -f"
echo ""
warn "For YouTube/Spotify downloads, send a fresh cookies.txt to the Telegram"
warn "bot chat. Export from your browser (logged into YouTube) using the"
warn "'Get cookies.txt LOCALLY' extension, then send the file to the bot."
echo ""
