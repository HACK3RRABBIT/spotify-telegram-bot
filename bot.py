#!/usr/bin/env python3
"""
Telegram bot that downloads Spotify tracks/albums using spotdl.
"""

import os
import re
import sys
import asyncio
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


async def start(update: Update, _) -> None:
    await update.message.reply_text(
        "Send me a Spotify track or album URL and I'll download it for you."
        "\n\nExamples:"
        "\n  https://open.spotify.com/track/..."
        "\n  https://open.spotify.com/album/..."
    )


async def handle_message(update: Update, _) -> None:
    url = update.message.text.strip()

    if not SPOTIFY_URL_PATTERN.match(url):
        await update.message.reply_text("Please send a valid Spotify URL.")
        return

    msg = await update.message.reply_text("Downloading...")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)

        cmd = [
            _SPOTDL_PATH,
            "--output", str(out_dir / "{title}"),
            "--print-errors",
            url,
        ]

        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

            logger.info(f"spotdl RC={proc.returncode}")
            if stdout:
                logger.info(f"spotdl stdout: {stdout.decode()[:1000]}")
            if stderr:
                logger.warning(f"spotdl stderr: {stderr.decode()[:1000]}")

            if proc.returncode != 0:
                error_text = (stderr or stdout).decode().strip()[:500]
                await msg.edit_text(f"Download failed:\n{error_text}")
                return

        except asyncio.TimeoutError:
            await msg.edit_text("Download timed out (10 min limit).")
            return
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error: {e}")
            return

        files = sorted(out_dir.iterdir())
        if not files:
            logger.warning("spotdl exited 0 but no files found in output dir")
            await msg.edit_text("No files were downloaded.")
            return

        await msg.edit_text("Uploading...")

        for f in files:
            with open(f, "rb") as fh:
                await update.message.reply_audio(
                    audio=fh,
                    title=f.stem,
                    filename=f.name,
                )

        count = len(files)
        await msg.edit_text(f"Sent {count} track{'s' if count > 1 else ''}.")


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
