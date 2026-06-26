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

# Supported URL patterns
URL_PATTERN = re.compile(
    r"https?://(open\.spotify\.com/(track|album|playlist|artist)/"
    r"|music\.youtube\.com/|youtube\.com/watch|youtu\.be/"
    r"|soundcloud\.com/)\S+",
    re.IGNORECASE,
)

# Locate spotdl: same conda env as this python, then PATH
_SPOTDL_PATH = os.path.join(os.path.dirname(sys.executable), "spotdl")
if not os.path.isfile(_SPOTDL_PATH):
    _SPOTDL_PATH = "spotdl"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Seconds between Telegram message edits (Telegram rate-limits to ~1/s per message)
_EDIT_INTERVAL = 3


def _parse_spotdl_line(line: str) -> dict:
    """Extract info from a spotdl --simple-tui output line."""
    info = {}
    # "Downloaded  track-name"  or  "Skipping  track-name"
    for keyword in ("Downloaded", "Skipping", "Downloading", "Converting", "Embed"):
        if keyword in line:
            info["event"] = keyword
            break
    # "Failed to download track-name"
    if "Failed" in line or "failed" in line:
        info["event"] = "Failed"
    # Percent: spotdl simple-tui prints lines like "10%|..." or "[10%]"
    m = re.search(r"(\d{1,3})%", line)
    if m:
        info["percent"] = int(m.group(1))
    return info


async def _run_spotdl(
    cmd: list[str], msg, timeout: int = 600
) -> tuple[int, str, list[str]]:
    """
    Run spotdl, stream output, update Telegram progress.
    Returns (returncode, full_output, downloaded_titles).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    all_lines: list[str] = []
    downloaded_titles: list[str] = []
    last_edit = asyncio.get_event_loop().time()
    downloaded = 0
    failed = 0
    current_percent: int | None = None
    current_track = ""

    async def read_lines():
        nonlocal downloaded, failed, current_percent, current_track
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            all_lines.append(line)
            logger.info(f"spotdl: {line}")

            info = _parse_spotdl_line(line)
            evt = info.get("event", "")

            if evt == "Downloading":
                # Extract track name after "Downloading  <name>"
                parts = line.split(None, 1)
                if len(parts) > 1:
                    current_track = parts[1].strip()
                current_percent = 0

            elif evt in ("Converting", "Embed"):
                current_percent = 99  # almost done

            elif evt == "Downloaded":
                downloaded += 1
                parts = line.split(None, 1)
                title = parts[1].strip() if len(parts) > 1 else current_track
                downloaded_titles.append(title)
                current_percent = None
                current_track = ""

            elif evt == "Failed":
                failed += 1

            if "percent" in info:
                current_percent = info["percent"]

    reader = asyncio.create_task(read_lines())
    deadline = asyncio.get_event_loop().time() + timeout

    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            await reader
            return -1, "\n".join(all_lines), downloaded_titles

        if now - last_edit >= _EDIT_INTERVAL:
            # Build status line
            if current_track:
                pct = f" {current_percent}%" if current_percent is not None else ""
                status = f"Downloading{pct}: `{current_track[:60]}`"
            else:
                status = f"Downloading... ({downloaded} done)"
            if failed:
                status += f", {failed} failed"
            try:
                await msg.edit_text(status, parse_mode="Markdown")
            except Exception:
                pass
            last_edit = now

        await asyncio.sleep(1)

    await reader
    rc = await proc.wait()
    return rc, "\n".join(all_lines), downloaded_titles


async def start(update: Update, _) -> None:
    await update.message.reply_text(
        "Send me a link and I'll download it for you.\n\n"
        "Supported sources:\n"
        "  Spotify — track, album, playlist, artist\n"
        "  YouTube — youtube.com/watch or youtu.be\n"
        "  SoundCloud — soundcloud.com/...\n\n"
        "Note: download speed depends on your server's internet connection."
    )


async def handle_message(update: Update, _) -> None:
    url = update.message.text.strip()

    if not URL_PATTERN.search(url):
        await update.message.reply_text(
            "Please send a Spotify, YouTube, or SoundCloud URL."
        )
        return

    msg = await update.message.reply_text("Starting download...")
    tmpdir = tempfile.mkdtemp(prefix="spotdl_")

    try:
        out_dir = Path(tmpdir)
        cmd = [
            _SPOTDL_PATH,
            "--output", str(out_dir) + "/{title}",
            "--simple-tui",
            "--print-errors",
            url,
        ]
        logger.info(f"Running: {' '.join(cmd)}")

        try:
            rc, output, downloaded_titles = await _run_spotdl(cmd, msg)
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error starting download: {e}")
            return

        if rc == -1:
            await msg.edit_text("Download timed out (10 min limit).")
            return

        files = sorted(f for f in out_dir.iterdir() if f.is_file())

        if not files:
            tail = "\n".join(output.splitlines()[-10:]) if output else "(no output)"
            logger.warning(f"spotdl exited {rc} but no files found.\n{output}")
            await msg.edit_text(
                f"No files were downloaded (exit {rc}).\n\nLast output:\n`{tail[:400]}`",
                parse_mode="Markdown",
            )
            return

        await msg.edit_text(f"Uploading {len(files)} track(s)...")

        sent_names: list[str] = []
        for f in files:
            try:
                with open(f, "rb") as fh:
                    await update.message.reply_audio(
                        audio=fh,
                        title=f.stem,
                        filename=f.name,
                    )
                sent_names.append(f.stem)
            finally:
                # Free disk space immediately after each upload
                try:
                    f.unlink()
                except OSError:
                    pass

        # Final summary with track names
        if sent_names:
            names_text = "\n".join(f"• {n}" for n in sent_names)
            await msg.edit_text(
                f"Sent {len(sent_names)} track(s):\n{names_text}"
            )
        else:
            await msg.edit_text("Done.")

    finally:
        # Always remove the temp dir, even on crash
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
