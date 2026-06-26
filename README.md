# Music Downloader Telegram Bot

A Telegram bot that downloads music from Spotify, YouTube, and SoundCloud — and sends the audio files directly to you.

Send a link, see the track details, then choose to download (or cancel). For YouTube videos you can pick audio quality before downloading.

## Features

- **Spotify** — track, album, playlist, artist
- **YouTube** — single video or full playlist (including YouTube Music)
- **SoundCloud** — track or set
- Confirm-before-download: see track info first, then decide
- YouTube quality selection: Best / Medium / Low
- Queue system: up to 3 simultaneous downloads; extra requests wait automatically
- Per-track progress for playlists on all platforms
- Skips files over Telegram's 49 MB limit with a warning

## Requirements

- Linux VPS (Ubuntu/Debian recommended)
- Conda (Miniconda)
- Telegram bot token — get one from [@BotFather](https://t.me/botfather)
- `ffmpeg` system package
- YouTube cookies file (Netscape format) for bot-detection bypass

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/HACK3RRABBIT/spotify-telegram-bot.git
cd spotify-telegram-bot
```

### 2. Create the conda environment

```bash
conda create -n spotify-downloader python=3.11 -y
conda activate spotify-downloader
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN
```

### 4. (Optional) YouTube cookie authentication

Export cookies from a browser session logged into YouTube (use a browser extension like *Get cookies.txt LOCALLY*) and save the Netscape-format file as `cookies.txt` in the bot directory. Without this the server IP may be blocked by YouTube's bot detection.

### 5. Run

```bash
conda run -n spotify-downloader python bot.py
```

Or as a systemd service:

```bash
sudo cp spotify-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-telegram-bot
```

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
├── cookies.txt                    # YouTube cookies (not committed)
├── .env                           # Bot token (not committed)
├── .env.example
├── spotify-telegram-bot.service   # systemd unit
├── install.sh
└── .github/workflows/deploy.yml  # Auto-deploy on push to main
```
