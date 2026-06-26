#!/usr/bin/env python3
"""
Telegram bot that downloads Spotify / YouTube / SoundCloud tracks.

Architecture:
  1. spotdl save  → fetch Spotify metadata (name, artist) — no download
  2. yt-dlp       → search YouTube Music and download directly
     (bypasses spotdl's yt-dlp integration which fails on server IPs)
"""

import json
import os
import re
import sys
import asyncio
import shutil
import tempfile
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

SPOTIFY_PATTERN = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|artist)/\S+",
    re.IGNORECASE,
)
DIRECT_PATTERN = re.compile(
    r"https?://(music\.youtube\.com/|youtube\.com/watch|youtu\.be/|soundcloud\.com/)\S+",
    re.IGNORECASE,
)

_CONDA_BIN   = os.path.dirname(sys.executable)
_SPOTDL_PATH = os.path.join(_CONDA_BIN, "spotdl")
if not os.path.isfile(_SPOTDL_PATH):
    _SPOTDL_PATH = "spotdl"
_YTDLP_PATH  = os.path.join(_CONDA_BIN, "yt-dlp")
if not os.path.isfile(_YTDLP_PATH):
    _YTDLP_PATH = "yt-dlp"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO, stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 3
_RE_PCT = re.compile(r"\[download\]\s+([\d.]+)%")


def _bar(pct: int) -> str:
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)


async def _spotdl_save(spotify_url: str, save_file: Path) -> list[dict]:
    """Use spotdl save to get Spotify metadata without downloading."""
    cmd = [_SPOTDL_PATH, "save", spotify_url, "--save-file", str(save_file)]
    logger.info("spotdl save: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    logger.info("spotdl save output: %s", out.decode(errors="replace")[:500])
    if not save_file.exists():
        return []
    data = json.loads(save_file.read_text())
    return data if isinstance(data, list) else [data]


async def _ytdlp_download(search_or_url: str, out_dir: Path, msg, title: str = "") -> list[Path]:
    """Download audio via yt-dlp with mobile player clients to bypass bot detection."""
    cmd = [
        _YTDLP_PATH,
        "--extractor-args", "youtube:player_client=android_vr,tv_embedded",
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--add-metadata",
        "--newline",
        "--no-playlist",
        "--output", str(out_dir / "%(title)s.%(ext)s"),
        search_or_url,
    ]
    logger.info("yt-dlp: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    last_edit = asyncio.get_event_loop().time()
    display = title or "track"

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        logger.info("yt-dlp: %s", line)
        m = _RE_PCT.search(line)
        if m:
            pct = min(int(float(m.group(1))), 100)
            now = asyncio.get_event_loop().time()
            if now - last_edit >= _EDIT_INTERVAL:
                try:
                    await msg.edit_text(
                        f"*{display}*\nDownloading... {pct}%  {_bar(pct)}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                last_edit = now

    await proc.wait()
    return sorted(f for f in out_dir.iterdir() if f.is_file())


async def start(update: Update, _) -> None:
    await update.message.reply_text(
        "Send me a link and I'll download it for you.\n\n"
        "Supported:\n"
        "  • Spotify — track, album, playlist, artist\n"
        "  • YouTube — youtube.com or youtu.be\n"
        "  • SoundCloud — soundcloud.com"
    )


async def handle_message(update: Update, _) -> None:
    url = update.message.text.strip()

    is_spotify = bool(SPOTIFY_PATTERN.search(url))
    is_direct  = bool(DIRECT_PATTERN.search(url))

    if not is_spotify and not is_direct:
        await update.message.reply_text(
            "Please send a Spotify, YouTube, or SoundCloud URL."
        )
        return

    msg = await update.message.reply_text("Looking up track...")
    tmpdir = tempfile.mkdtemp(prefix="spotdl_")

    try:
        out_dir = Path(tmpdir)
        files: list[Path] = []
        sent_titles: list[str] = []

        if is_direct:
            # YouTube or SoundCloud URL — download directly
            await msg.edit_text("Downloading...")
            files = await _ytdlp_download(url, out_dir, msg)

        else:
            # Spotify URL — resolve metadata first, then download via yt-dlp
            save_file = out_dir / "meta.spotdl"
            try:
                songs = await _spotdl_save(url, save_file)
            except asyncio.TimeoutError:
                await msg.edit_text("Timed out fetching Spotify metadata (>3 min).")
                return
            except Exception as e:
                logger.exception("spotdl save failed")
                await msg.edit_text(f"Error fetching track info: {e}")
                return

            if not songs:
                await msg.edit_text("Could not find track on Spotify.")
                return

            for i, song in enumerate(songs, 1):
                name   = song.get("name", "")
                artist = song.get("artist", "")
                title  = f"{artist} - {name}" if artist else name

                if len(songs) > 1:
                    await msg.edit_text(
                        f"Downloading {i}/{len(songs)}: *{title}*",
                        parse_mode="Markdown",
                    )
                else:
                    await msg.edit_text(
                        f"Found: *{title}*\nDownloading... 0%  {_bar(0)}",
                        parse_mode="Markdown",
                    )

                track_dir = out_dir / f"track_{i}"
                track_dir.mkdir()

                # Search YouTube Music, fall back to YouTube
                search = f"ytmsearch1:{title}"
                new_files = await _ytdlp_download(search, track_dir, msg, title)
                if not new_files:
                    search = f"ytsearch1:{title}"
                    new_files = await _ytdlp_download(search, track_dir, msg, title)

                if new_files:
                    files.extend(new_files)
                    sent_titles.append(title)
                else:
                    logger.warning("No file for: %s", title)

        if not files:
            await msg.edit_text("No files were downloaded. The track may not be available.")
            return

        await msg.edit_text("Uploading...")

        for f in files:
            try:
                with open(f, "rb") as fh:
                    await update.message.reply_audio(
                        audio=fh,
                        title=f.stem,
                        filename=f.name,
                    )
            finally:
                try:
                    f.unlink()
                except OSError:
                    pass

        titles = sent_titles or [f.stem for f in files]
        if len(titles) == 1:
            await msg.edit_text(f"✓ *{titles[0]}*", parse_mode="Markdown")
        else:
            bullet = "\n".join(f"• {t}" for t in titles)
            await msg.edit_text(f"✓ {len(titles)} tracks:\n{bullet}", parse_mode="Markdown")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def error_handler(update: Update, context) -> None:
    logger.error("Update %s caused error %s", update, context.error)


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
