#!/usr/bin/env python3
"""
Telegram bot that downloads Spotify tracks/albums using spotdl.
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
SPOTIFY_URL_PATTERN = re.compile(r"https?://open\.spotify\.com/(track|album)/\S+")

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

# How often (seconds) to push progress edits to Telegram
_PROGRESS_INTERVAL = 4


async def start(update: Update, _) -> None:
    await update.message.reply_text(
        "Send me a Spotify track or album URL and I'll download it for you."
        "\n\nExamples:"
        "\n  https://open.spotify.com/track/..."
        "\n  https://open.spotify.com/album/..."
    )


async def _run_spotdl(cmd: list[str], msg, timeout: int = 600) -> tuple[int, str]:
    """Run spotdl and stream progress to Telegram. Returns (returncode, full_output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge so we catch all output
    )

    lines: list[str] = []
    last_edit = asyncio.get_event_loop().time()
    downloaded = 0
    failed = 0

    async def read_lines():
        nonlocal downloaded, failed
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            lines.append(line)
            logger.info(f"spotdl: {line}")
            if "Downloaded" in line:
                downloaded += 1
            if "Failed" in line or "Error" in line.capitalize():
                failed += 1

    reader = asyncio.create_task(read_lines())

    # Periodically update the Telegram message with current progress
    deadline = asyncio.get_event_loop().time() + timeout
    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            await reader
            return -1, "\n".join(lines)

        if now - last_edit >= _PROGRESS_INTERVAL:
            status = f"Downloading... {downloaded} done"
            if failed:
                status += f", {failed} failed"
            if lines:
                # Show last meaningful line as hint
                last = next(
                    (l for l in reversed(lines) if l.strip() and not l.startswith("[")),
                    lines[-1],
                )
                status += f"\n`{last[:120]}`"
            try:
                await msg.edit_text(status, parse_mode="Markdown")
            except Exception:
                pass  # Telegram may throttle edits; that's fine
            last_edit = now

        await asyncio.sleep(1)

    await reader
    rc = await proc.wait()
    return rc, "\n".join(lines)


async def handle_message(update: Update, _) -> None:
    url = update.message.text.strip()

    if not SPOTIFY_URL_PATTERN.match(url):
        await update.message.reply_text("Please send a valid Spotify URL.")
        return

    msg = await update.message.reply_text("Starting download...")
    tmpdir = tempfile.mkdtemp(prefix="spotdl_")

    try:
        out_dir = Path(tmpdir)
        cmd = [
            _SPOTDL_PATH,
            "--output", str(out_dir) + "/{title}",
            "--print-errors",
            url,
        ]

        logger.info(f"Running: {' '.join(cmd)}")

        try:
            rc, output = await _run_spotdl(cmd, msg)
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error starting download: {e}")
            return

        if rc == -1:
            await msg.edit_text("Download timed out (10 min limit).")
            return

        files = sorted(
            f for f in out_dir.iterdir() if f.is_file()
        )

        if not files:
            tail = "\n".join(output.splitlines()[-10:]) if output else "(no output)"
            logger.warning(f"spotdl exited {rc} but no files found.\n{output}")
            await msg.edit_text(
                f"No files were downloaded (exit code {rc}).\n\nLast output:\n`{tail[:400]}`",
                parse_mode="Markdown",
            )
            return

        await msg.edit_text(f"Uploading {len(files)} track(s)...")

        for f in files:
            try:
                with open(f, "rb") as fh:
                    await update.message.reply_audio(
                        audio=fh,
                        title=f.stem,
                        filename=f.name,
                    )
            finally:
                # Delete each file immediately after upload to free disk space
                try:
                    f.unlink()
                except OSError:
                    pass

        count = len(files)
        await msg.edit_text(f"Sent {count} track{'s' if count > 1 else ''}.")

    finally:
        # Always clean up the temp directory, even if we crash mid-download
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
