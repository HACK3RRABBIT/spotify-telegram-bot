# Spotify Telegram Bot

A Telegram bot that downloads music from Spotify URLs using [spotdl](https://github.com/spotdl/spotify-downloader).

Send a Spotify track or album link to the bot, and it replies with the audio file.

## Features

- Download single tracks or full albums
- Sends audio files via Telegram
- Runs as a systemd service on any Linux VPS
- Uses Conda environment for isolated dependencies

## Requirements

- Linux VPS (Ubuntu/Debian recommended)
- Conda (Miniconda)
- Telegram bot token (get one from [@BotFather](https://t.me/botfather))

## Quick Install

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/spotify-telegram-bot/main/install.sh)"
```

During install you'll be prompted for your bot token. After that the service starts automatically.

## Manual Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/spotify-telegram-bot.git
cd spotify-telegram-bot
```

### 2. Install dependencies

```bash
# System deps
sudo apt install ffmpeg

# Create conda env (or use your existing spotify-downloader env)
conda create -n spotify-downloader python=3.11 -y
conda activate spotify-downloader

# Install spotdl and bot dependencies
pip install spotdl python-telegram-bot python-dotenv
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN
```

### 4. Run manually

```bash
conda run -n spotify-downloader python bot.py
```

### 5. Run as a service (systemd)

```bash
sudo ./install.sh
```

Or manually:

```bash
sudo cp spotify-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-telegram-bot
```

## Usage

1. Start a chat with your bot on Telegram
2. Send `/start`
3. Send a Spotify URL:
   - Track: `https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT`
   - Album: `https://open.spotify.com/album/1kfVWZRg3nL3PmZ3Gd8k2H`
4. Wait for the bot to download and send the audio

## Commands

| Command  | Description          |
|----------|----------------------|
| `/start` | Show help message    |

## Service Management

```bash
# Status
systemctl status spotify-telegram-bot

# Logs
journalctl -u spotify-telegram-bot -f

# Restart
sudo systemctl restart spotify-telegram-bot

# Stop
sudo systemctl stop spotify-telegram-bot
```

## Project Structure

```
├── bot.py                      # Main bot logic
├── requirements.txt            # Python dependencies
├── install.sh                  # Automated VPS installer
├── spotify-telegram-bot.service  # systemd unit file
├── .env.example                # Token configuration template
├── .gitignore
└── README.md
```

## License

MIT
