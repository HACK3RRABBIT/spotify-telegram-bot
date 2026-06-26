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

_EDIT_INTERVAL = 3  # seconds between Telegram message edits

# spotdl --simple-tui output patterns (confirmed from live test):
#   "Artist - Title: Downloading"
#   "Artist - Title: Embedding metadata"
#   "Artist - Title: Done"
#   'Downloaded "Artist - Title":'
#   "N/M complete"
_RE_STAGE    = re.compile(r"^(.+?):\s+(Downloading|Embedding metadata|Done|Converting|Failed.*)$")
_RE_DOWNLOADED = re.compile(r'^Downloaded\s+"(.+?)"')
_RE_PROGRESS = re.compile(r"^(\d+)/(\d+) complete$")


def _bar(filled: int, total: int = 10) -> str:
    return "▓" * filled + "░" * (total - filled)


# Stage → (display label, progress out of 10)
_STAGE_MAP = {
    "Downloading":        ("Downloading",        4),
    "Converting":         ("Converting",         7),
    "Embedding metadata": ("Embedding metadata", 9),
    "Done":               ("Done",              10),
}


async def _run_spotdl(
    cmd: list[str], msg, timeout: int = 600
) -> tuple[int, str, list[str]]:
    """Stream spotdl output; update Telegram with stage progress."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    all_lines: list[str] = []
    downloaded_titles: list[str] = []
    last_edit = asyncio.get_event_loop().time()

    state = {"text": "Looking up track..."}

    async def read_lines():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            all_lines.append(line)
            logger.info("spotdl: %s", line)

            # "Artist - Title: Downloading" / "...Done" / etc.
            m = _RE_STAGE.match(line)
            if m:
                track, stage = m.group(1).strip(), m.group(2)
                label, filled = _STAGE_MAP.get(stage, (stage, 5))
                pct = filled * 10
                state["text"] = (
                    f"*{track}*\n"
                    f"{label}... {pct}%  {_bar(filled)}"
                )
                continue

            # 'Downloaded "Artist - Title":'
            m = _RE_DOWNLOADED.match(line)
            if m:
                title = m.group(1).strip()
                downloaded_titles.append(title)
                state["text"] = f"✓ *{title}*\nUploading..."
                continue

            # "1/3 complete"  (album progress)
            m = _RE_PROGRESS.match(line)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                state["text"] = (
                    f"Downloaded {done}/{total} tracks\n"
                    f"{_bar(done * 10 // total)}"
                )

    reader = asyncio.create_task(read_lines())
    deadline = asyncio.get_event_loop().time() + timeout

    while not reader.done():
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            proc.kill()
            await reader
            return -1, "\n".join(all_lines), downloaded_titles

        if now - last_edit >= _EDIT_INTERVAL:
            try:
                await msg.edit_text(state["text"], parse_mode="Markdown")
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
            "--overwrite", "force",       # never skip due to cache
            "--simple-tui",               # parseable line-by-line output
            "--yt-dlp-args=--newline",    # yt-dlp progress on separate lines
            "--audio", "youtube-music", "youtube", "soundcloud",  # fallback sources
            "--dont-filter-results",      # don't reject non-latin / low-score matches
            url,
        ]
        logger.info("Running: %s", " ".join(cmd))

        try:
            rc, output, downloaded_titles = await _run_spotdl(cmd, msg)
        except Exception as e:
            logger.exception("Subprocess error")
            await msg.edit_text(f"Error: {e}")
            return

        if rc == -1:
            await msg.edit_text("Download timed out (10 min limit).")
            return

        files = sorted(f for f in out_dir.iterdir() if f.is_file())

        if not files:
            tail = "\n".join(output.splitlines()[-15:]) if output else "(no output)"
            logger.warning("spotdl exited %d but no files found.", rc)
            await msg.edit_text(
                f"No files were downloaded (exit {rc}).\n\n`{tail[:400]}`",
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
                # Delete immediately after upload — free disk space track by track
                try:
                    f.unlink()
                except OSError:
                    pass

        # Final message: only the track name(s)
        titles = downloaded_titles or [f.stem for f in files]
        if len(titles) == 1:
            await msg.edit_text(f"✓ *{titles[0]}*", parse_mode="Markdown")
        else:
            bullet = "\n".join(f"• {t}" for t in titles)
            await msg.edit_text(
                f"✓ {len(titles)} tracks:\n{bullet}", parse_mode="Markdown"
            )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def error_handler(update: Update, context) -> None:
    logger.error("Update %s caused error %s", update, context.error)


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
