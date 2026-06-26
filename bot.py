#!/usr/bin/env python3
"""
Telegram bot that downloads Spotify / YouTube / SoundCloud tracks using spotdl.
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

_SPOTDL_PATH = os.path.join(os.path.dirname(sys.executable), "spotdl")
if not os.path.isfile(_SPOTDL_PATH):
    _SPOTDL_PATH = "spotdl"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 3  # seconds between Telegram edits


async def _run_spotdl(
    cmd: list[str], msg, timeout: int = 600
) -> tuple[int, str, list[str]]:
    """
    Run spotdl, stream output line by line, update Telegram message with:
      - track name as soon as spotdl finds it
      - real download percentage from yt-dlp
    Returns (returncode, full_output, list_of_sent_titles).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    all_lines: list[str] = []
    sent_titles: list[str] = []
    last_edit_time = asyncio.get_event_loop().time()

    # Mutable state updated by the reader coroutine
    state = {
        "current_track": "",   # name of track being downloaded right now
        "percent": None,       # int 0-100 or None
        "done_count": 0,
        "fail_count": 0,
        "status_text": "Looking up track...",
    }

    # Regex to catch yt-dlp progress: "[download]  45.2% of 4.50MiB ..."
    _YTDLP_PCT = re.compile(r"\[download\]\s+([\d.]+)%")
    # spotdl simple-tui: "Downloading  Artist - Title"
    _DOWNLOADING = re.compile(r"^Downloading\s{1,3}(.+)$")
    # spotdl simple-tui: "Downloaded  Artist - Title"
    _DOWNLOADED  = re.compile(r"^Downloaded\s{1,3}(.+)$")
    # "Failed to download ..."
    _FAILED      = re.compile(r"Failed", re.IGNORECASE)

    async def read_lines():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            all_lines.append(line)
            logger.info(f"spotdl: {line}")

            m = _DOWNLOADING.match(line)
            if m:
                state["current_track"] = m.group(1).strip()
                state["percent"] = 0
                state["status_text"] = (
                    f'Found: *{state["current_track"]}*\nDownloading... 0%'
                )
                continue

            m = _YTDLP_PCT.search(line)
            if m and state["current_track"]:
                pct = int(float(m.group(1)))
                state["percent"] = pct
                state["status_text"] = (
                    f'Downloading: *{state["current_track"]}*\n'
                    f'Progress: {pct}%  {"▓" * (pct // 10)}{"░" * (10 - pct // 10)}'
                )
                continue

            m = _DOWNLOADED.match(line)
            if m:
                title = m.group(1).strip()
                sent_titles.append(title)
                state["done_count"] += 1
                state["percent"] = 100
                state["status_text"] = (
                    f'✓ Downloaded: *{title}*\nUploading...'
                )
                continue

            if _FAILED.search(line):
                state["fail_count"] += 1
                state["status_text"] = f'⚠️ Failed: `{line[:100]}`'

    reader = asyncio.create_task(read_lines())
    deadline = asyncio.get_event_loop().time() + timeout

    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            await reader
            return -1, "\n".join(all_lines), sent_titles

        if now - last_edit_time >= _EDIT_INTERVAL:
            try:
                await msg.edit_text(state["status_text"], parse_mode="Markdown")
            except Exception:
                pass
            last_edit_time = now

        await asyncio.sleep(1)

    await reader
    rc = await proc.wait()
    return rc, "\n".join(all_lines), sent_titles


async def start(update: Update, _) -> None:
    await update.message.reply_text(
        "Send me a link and I'll download it for you.\n\n"
        "Supported:\n"
        "  • Spotify — track, album, playlist, artist\n"
        "  • YouTube — youtube.com or youtu.be\n"
        "  • SoundCloud — soundcloud.com\n\n"
        "Tip: download speed depends on the server's internet connection."
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
            "--simple-tui",
            "--log-level", "DEBUG",
            "--print-errors",
            url,
        ]
        logger.info(f"Running: {' '.join(cmd)}")

        try:
            rc, output, sent_titles = await _run_spotdl(cmd, msg)
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error starting download: {e}")
            return

        if rc == -1:
            await msg.edit_text("Download timed out (10 min limit).")
            return

        files = sorted(f for f in out_dir.iterdir() if f.is_file())

        if not files:
            tail = "\n".join(output.splitlines()[-15:]) if output else "(no output)"
            logger.warning(f"spotdl exited {rc} but no files found.\n{output}")
            await msg.edit_text(
                f"No files were downloaded (exit {rc}).\n\nLast output:\n`{tail[:400]}`",
                parse_mode="Markdown",
            )
            return

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

        # Final message: just the track name(s), no generic "Sent N tracks"
        if sent_titles:
            if len(sent_titles) == 1:
                await msg.edit_text(f"✓ *{sent_titles[0]}*", parse_mode="Markdown")
            else:
                names = "\n".join(f"• {t}" for t in sent_titles)
                await msg.edit_text(f"✓ Sent {len(sent_titles)} tracks:\n{names}", parse_mode="Markdown")
        else:
            # Fallback: use filenames if titles weren't captured from output
            names = "\n".join(f"• {f.stem}" for f in files)
            if len(files) == 1:
                await msg.edit_text(f"✓ *{files[0].stem}*", parse_mode="Markdown")
            else:
                await msg.edit_text(f"✓ Sent {len(files)} tracks:\n{names}", parse_mode="Markdown")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def error_handler(update: Update, context) -> None:
    logger.error(f"Update {update} caused error {context.error}")


def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN not set. Create a .env file or export it."
        )
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
