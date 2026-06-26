# Music Downloader Telegram Bot

A Telegram bot that downloads music from Spotify, YouTube, and SoundCloud and sends the audio files directly to you.

Send a link, see track details, choose to download (or cancel). YouTube links offer quality selection. Playlists download per-track with progress updates.

## Features

- **Spotify** — track, album, playlist, artist
- **YouTube** — single video or full playlist (including YouTube Music links)
- **SoundCloud** — track or set/playlist
- Confirm-before-download dialog with track info
- YouTube quality selection: Best / Medium / Low
- Queue system: up to 3 simultaneous downloads
- Per-track progress for all platforms
- Skips files over Telegram's 49 MB limit with a warning
- Upload `cookies.txt` via Telegram (no SSH needed) to refresh YouTube auth

## How YouTube blocking is solved

VPS servers have datacenter IP addresses (DigitalOcean, AWS, Hetzner, etc.) that YouTube blocks at the IP level. All requests return `LOGIN_REQUIRED` regardless of cookies or user-agent.

This bot uses a two-layer solution:

```
bot  →  yt-dlp / spotdl
              ↓
        Cloudflare WARP  (SOCKS5 on 127.0.0.1:40000)
              ↓
        Cloudflare edge network  (exit IP: AS13335 — not blocked by YouTube)
              ↓
        YouTube / YouTube Music
```

Additionally, **bgutil-ytdlp-pot-provider** generates YouTube Proof-of-Origin (PO) tokens on demand. These tokens prove the request comes from a browser-like client and satisfy YouTube's bot-detection challenges — the same challenge a browser would solve.

**Result:** The server's VPS IP is never seen by YouTube. The Cloudflare exit IP + bgutil PO token combination allows downloads without a paid residential proxy service.

**Cookie requirement:** For most content (music videos, playlists, Spotify-sourced tracks), YouTube still requires a logged-in session. Export fresh cookies from your browser every few months and send the `.txt` file to the bot in Telegram — it updates automatically.

## Test results

| Platform | Type | Status | Notes |
|---|---|---|---|
| YouTube | Single | ✅ | bgutil PO token via Cloudflare WARP |
| YouTube | Playlist | ✅ (with cookies) | Requires fresh cookies.txt |
| Spotify | Single | ✅ (with cookies) | Searches YouTube; requires cookies |
| Spotify | Album | ✅ (with cookies) | Per-track; requires cookies |
| Spotify | Playlist | ✅ (with cookies) | Per-track; requires cookies |
| SoundCloud | Single | ✅ | No auth required |
| SoundCloud | Set/Playlist | ✅ | No auth required |

## Requirements

- Linux VPS (Ubuntu 22.04 / 24.04 recommended)
- Root access
- Telegram bot token — get one from [@BotFather](https://t.me/botfather)
- `ffmpeg` (installed automatically)

## Quick install

```bash
git clone https://github.com/HACK3RRABBIT/spotify-telegram-bot.git
cd spotify-telegram-bot
sudo bash install.sh
```

The installer automatically:
1. Installs `cloudflare-warp` and registers it in proxy mode (SOCKS5 on port 40000)
2. Installs `privoxy` as an HTTP→SOCKS5 bridge (HTTP on port 8118, for spotdl compatibility)
3. Configures both to auto-start on boot
4. Creates the conda environment and installs all Python packages
5. Installs the bgutil Deno PO-token server
6. Configures `.env` with `YTDLP_PROXY` and `SPOTDL_PROXY`
7. Installs and starts the systemd service

## Manual setup

### 1. Clone the repo

```bash
git clone https://github.com/HACK3RRABBIT/spotify-telegram-bot.git
cd spotify-telegram-bot
```

### 2. Create the conda environment

```bash
conda create -n spotify-downloader python=3.11 -y
conda activate spotify-downloader
pip install python-telegram-bot python-dotenv spotdl httpx bgutil-ytdlp-pot-provider
```

### 3. Set up Cloudflare WARP

```bash
# Install WARP
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
  | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] \
  https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
  | tee /etc/apt/sources.list.d/cloudflare-client.list
apt-get update && apt-get install -y cloudflare-warp

# Register and set proxy mode (no account needed)
systemctl start warp-svc
warp-cli --accept-tos registration new
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

# Verify exit IP is Cloudflare
curl --proxy socks5://127.0.0.1:40000 https://ipinfo.io/json
```

### 4. Set up privoxy (HTTP bridge for spotdl)

```bash
apt-get install -y privoxy
echo "forward-socks5 / 127.0.0.1:40000 ." >> /etc/privoxy/config
systemctl restart privoxy
```

### 5. Configure

```bash
cp .env.example .env
# Edit .env and set:
#   TELEGRAM_BOT_TOKEN=your_token_here
#   YTDLP_PROXY=socks5://127.0.0.1:40000
#   SPOTDL_PROXY=http://127.0.0.1:8118
```

### 6. Run

```bash
conda run -n spotify-downloader python bot.py
```

Or as a systemd service:

```bash
sudo cp spotify-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-telegram-bot
```

## Refreshing YouTube cookies

YouTube session cookies expire every few months. When downloads start failing with authentication errors, refresh them:

1. Open [youtube.com](https://youtube.com) in your browser while logged in to your Google account
2. Install the **"Get cookies.txt LOCALLY"** browser extension
3. Click the extension → **Export** — this saves `cookies.txt`
4. Send the `cookies.txt` file directly to the Telegram bot chat
5. The bot replies "✅ cookies.txt updated!" — downloads work again immediately

No SSH access needed.

## Service commands

```bash
systemctl status spotify-telegram-bot
journalctl -u spotify-telegram-bot -f
sudo systemctl restart spotify-telegram-bot
```

## Project structure

```
├── bot.py                         # Main bot
├── requirements.txt               # Python dependencies
├── install.sh                     # Full installer (WARP + privoxy + bot)
├── cookies.txt                    # YouTube cookies (not committed)
├── .env                           # Bot token + proxy config (not committed)
├── .env.example
├── spotify-telegram-bot.service   # systemd unit
└── .github/workflows/deploy.yml  # Auto-deploy on push to main
```

## Proxy architecture detail

```
yt-dlp:
  --proxy socks5://127.0.0.1:40000  →  WARP SOCKS5  →  Cloudflare edge  →  YouTube

spotdl:
  --proxy http://127.0.0.1:8118     →  privoxy HTTP  →  WARP SOCKS5  →  Cloudflare edge  →  YouTube

bgutil PO token generation:
  Deno script  →  -p socks5h://127.0.0.1:40000  →  WARP  →  YouTube (generates token)
  yt-dlp sends token in request headers  →  YouTube accepts  →  download proceeds
```
