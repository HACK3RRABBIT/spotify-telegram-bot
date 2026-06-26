#!/usr/bin/env python3
"""
Telegram bot that downloads Spotify / YouTube / SoundCloud tracks using spotdl.
Falls back to yt-dlp directly when spotdl hits YouTube bot-detection errors.
"""

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

URL_PATTERN = re.compile(
    r"https?://(open\.spotify\.com/(track|album|playlist|artist)/"
    r"|music\.youtube\.com/|youtube\.com/watch|youtu\.be/"
    r"|soundcloud\.com/)\S+",
    re.IGNORECASE,
)

_CONDA_BIN  = os.path.dirname(sys.executable)
_SPOTDL_PATH = os.path.join(_CONDA_BIN, "spotdl")
if not os.path.isfile(_SPOTDL_PATH):
    _SPOTDL_PATH = "spotdl"

_YTDLP_PATH = os.path.join(_CONDA_BIN, "yt-dlp")
if not os.path.isfile(_YTDLP_PATH):
    _YTDLP_PATH = "yt-dlp"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 3

_RE_STAGE      = re.compile(r"^(.+?):\s+(Downloading|Embedding metadata|Done|Converting|Failed.*)$")
_RE_DOWNLOADED = re.compile(r'^Downloaded\s+"(.+?)"')
_RE_PROGRESS   = re.compile(r"^(\d+)/(\d+) complete$")
# AudioProviderError line contains the YouTube URL we can retry
_RE_PROVIDER_ERR = re.compile(r"AudioProviderError.*?(https?://\S+)")

_STAGE_MAP = {
    "Downloading":        ("Downloading",        4),
    "Converting":         ("Converting",         7),
    "Embedding metadata": ("Embedding metadata", 9),
    "Done":               ("Done",              10),
}


def _bar(filled: int, total: int = 10) -> str:
    return "▓" * filled + "░" * (total - filled)


async def _stream(proc, on_line):
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            on_line(line)


async def _run_spotdl(cmd, msg, timeout=600):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    all_lines = []
    downloaded_titles = []
    failed_yt_urls = []
    last_edit = asyncio.get_event_loop().time()
    state = {"text": "Looking up track...", "prev": ""}

    def on_line(line):
        all_lines.append(line)
        logger.info("spotdl: %s", line)

        # Error and URL may be on consecutive lines:
        #   "AudioProviderError: YT-DLP download error -"
        #   "https://music.youtube.com/watch?v=..."
        if line.startswith("https://") and "AudioProviderError" in state["prev"]:
            failed_yt_urls.append(line.strip())
            state["text"] = "⚠️ YouTube blocked — retrying with fallback..."
            state["prev"] = line
            return

        # Also handle case where URL is on the same line
        m = _RE_PROVIDER_ERR.search(line)
        if m:
            failed_yt_urls.append(m.group(1))
            state["text"] = "⚠️ YouTube blocked — retrying with fallback..."
            state["prev"] = line
            return

        state["prev"] = line
        m = _RE_STAGE.match(line)
        if m:
            track, stage = m.group(1).strip(), m.group(2)
            label, filled = _STAGE_MAP.get(stage, (stage, 5))
            state["text"] = f"*{track}*\n{label}... {filled*10}%  {_bar(filled)}"
            return

        m = _RE_DOWNLOADED.match(line)
        if m:
            title = m.group(1).strip()
            downloaded_titles.append(title)
            state["text"] = f"✓ *{title}*\nUploading..."
            return

        m = _RE_PROGRESS.match(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            state["text"] = f"Downloaded {done}/{total} tracks\n{_bar(done * 10 // total)}"
            return

    reader = asyncio.create_task(_stream(proc, on_line))
    deadline = asyncio.get_event_loop().time() + timeout

    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            await reader
            return -1, "\n".join(all_lines), downloaded_titles, failed_yt_urls
        if now - last_edit >= _EDIT_INTERVAL:
            try:
                await msg.edit_text(state["text"], parse_mode="Markdown")
            except Exception:
                pass
            last_edit = now
        await asyncio.sleep(1)

    await reader
    rc = await proc.wait()
    return rc, "\n".join(all_lines), downloaded_titles, failed_yt_urls


async def _ytdlp_fallback(yt_url: str, out_dir: Path, msg, timeout=300) -> list[Path]:
    """
    Download a YouTube/YT-Music URL directly via yt-dlp using player clients
    that bypass server-side bot detection (android_vr, tv_embedded).
    Returns list of downloaded files.
    """
    await msg.edit_text("YouTube blocked spotdl — retrying directly with yt-dlp...")

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
        "--output", str(out_dir / "%(title)s.%(ext)s"),
        yt_url,
    ]
    logger.info("yt-dlp fallback: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    all_lines = []
    last_edit = asyncio.get_event_loop().time()
    state = {"text": "Retrying download..."}

    _RE_PCT = re.compile(r"\[download\]\s+([\d.]+)%")

    def on_line(line):
        all_lines.append(line)
        logger.info("yt-dlp: %s", line)
        m = _RE_PCT.search(line)
        if m:
            pct = min(int(float(m.group(1))), 100)
            filled = pct // 10
            state["text"] = f"Downloading (fallback)...\n{pct}%  {_bar(filled)}"

    reader = asyncio.create_task(_stream(proc, on_line))
    deadline = asyncio.get_event_loop().time() + timeout

    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            break
        if now - last_edit >= _EDIT_INTERVAL:
            try:
                await msg.edit_text(state["text"], parse_mode="Markdown")
            except Exception:
                pass
            last_edit = now
        await asyncio.sleep(1)

    await reader
    await proc.wait()

    logger.info("yt-dlp output:\n%s", "\n".join(all_lines))
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

    if not URL_PATTERN.search(url):
        await update.message.reply_text(
            "Please send a Spotify, YouTube, or SoundCloud URL."
        )
        return

    msg = await update.message.reply_text("Looking up track...")
    tmpdir = tempfile.mkdtemp(prefix="spotdl_")

    try:
        out_dir = Path(tmpdir)
        cmd = [
            _SPOTDL_PATH,
            "--output", str(out_dir) + "/{title}",
            "--overwrite", "force",
            "--simple-tui",
            "--yt-dlp-args=--newline",
            "--audio", "youtube-music", "youtube", "soundcloud", "piped",
            "--dont-filter-results",
            url,
        ]
        logger.info("Running spotdl: %s", " ".join(cmd))

        try:
            rc, output, downloaded_titles, failed_yt_urls = await _run_spotdl(cmd, msg)
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error: {e}")
            return

        if rc == -1:
            await msg.edit_text("Download timed out (10 min limit).")
            return

        files = sorted(f for f in out_dir.iterdir() if f.is_file())

        # If spotdl failed due to YouTube bot-detection, retry with yt-dlp directly
        if not files and failed_yt_urls:
            for yt_url in failed_yt_urls:
                files = await _ytdlp_fallback(yt_url, out_dir, msg)
                if files:
                    break

        if not files:
            tail = "\n".join(output.splitlines()[-15:]) if output else "(no output)"
            logger.warning("No files after all attempts. spotdl exit=%d", rc)
            await msg.edit_text(
                f"No files were downloaded.\n\n`{tail[:400]}`",
                parse_mode="Markdown",
            )
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

        titles = downloaded_titles or [f.stem for f in files]
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
