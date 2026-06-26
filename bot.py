#!/usr/bin/env python3
"""
Telegram music downloader — Spotify · YouTube · SoundCloud

Flow per request:
  1. Quick metadata fetch (no download)
  2. Confirm dialog with inline buttons (quality choice for YouTube singles)
  3. Slot acquired from queue → download → upload one-by-one → summary
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── Config ────────────────────────────────────────────────────────────────────
TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
MAX_CONCURRENT = 3          # simultaneous downloads
MAX_FILE_BYTES = 49 << 20  # 49 MB — Telegram bot upload limit
CONFIRM_TTL    = 300        # seconds a confirm button stays active

# Optional residential proxy to bypass YouTube's server IP block.
# Set YTDLP_PROXY in .env to a proxy URL, e.g.:
#   YTDLP_PROXY=http://user:pass@proxy.webshare.io:80
#   YTDLP_PROXY=socks5://user:pass@gate.smartproxy.com:7000
# Leave unset to connect directly (YouTube may block the server IP).
YTDLP_PROXY  = os.environ.get("YTDLP_PROXY", "").strip()
# spotdl only accepts HTTP/HTTPS proxies; privoxy bridges HTTP→WARP SOCKS5
SPOTDL_PROXY = os.environ.get("SPOTDL_PROXY", "").strip()


# ── Binary / path resolution ──────────────────────────────────────────────────
def _find_bin(name: str) -> str:
    p = os.path.join(os.path.dirname(sys.executable), name)
    return p if os.path.isfile(p) else name

_SPOTDL  = _find_bin("spotdl")
_YTDLP   = _find_bin("yt-dlp")
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_COOKIES = os.path.join(_BOT_DIR, "cookies.txt")


# ── URL patterns ──────────────────────────────────────────────────────────────
_RE_SPOTIFY    = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|artist)/\S+", re.I)
_RE_YOUTUBE    = re.compile(
    r"https?://(?:(?:www\.|music\.)?youtube\.com|youtu\.be)/\S+", re.I)
_RE_SOUNDCLOUD = re.compile(
    r"https?://(?:www\.)?soundcloud\.com/\S+", re.I)


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    level=logging.INFO, stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Global state ──────────────────────────────────────────────────────────────
_sem     = asyncio.Semaphore(MAX_CONCURRENT)
_active  = 0                        # currently downloading
_pending: dict[str, dict] = {}     # confirm token → context


# ── Utility ───────────────────────────────────────────────────────────────────
_RE_PCT      = re.compile(r"\[download\]\s+([\d.]+)%")
_RE_ITEM     = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
_RE_SPOTPROG = re.compile(r"(\d+)/(\d+) complete")


def _bar(pct: int) -> str:
    f = pct // 10
    return "▓" * f + "░" * (10 - f)


def _dur(s) -> str:
    try:
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "?"


def _cookie_args() -> list[str]:
    args = []
    if os.path.isfile(_COOKIES):
        args += ["--cookies", _COOKIES]
    if YTDLP_PROXY:
        args += ["--proxy", YTDLP_PROXY]
    return args


def _spotdl_proxy_args() -> list[str]:
    return ["--proxy", SPOTDL_PROXY] if SPOTDL_PROXY else []


async def _run(cmd: list[str], timeout: int = 60) -> tuple[str, int]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", -1
    return out.decode(errors="replace"), proc.returncode


def _clean_pending() -> None:
    now = time.time()
    for k in [k for k, v in list(_pending.items()) if v["expires"] < now]:
        _pending.pop(k, None)


# ── Quick metadata fetch (no download) ───────────────────────────────────────

async def _fetch_yt_sc_info(url: str) -> dict | None:
    """Fetch title / count from YouTube or SoundCloud without downloading.

    stderr is sent to DEVNULL so yt-dlp's cookie-loading messages don't
    corrupt the JSON that comes on stdout.
    For YouTube, falls back to the public oEmbed API if yt-dlp is blocked.
    """
    proc = await asyncio.create_subprocess_exec(
        *([_YTDLP] + _cookie_args() + ["--flat-playlist", "-J", "--no-warnings", url]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        out_bytes = b""

    out = out_bytes.decode(errors="replace").strip()
    try:
        d = json.loads(out)
    except Exception:
        d = None

    if d is None:
        logger.warning("yt-dlp -J failed for %s — trying oEmbed fallback", url)
        return await _fetch_oembed_fallback(url)

    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        return {"kind": "playlist", "count": len(entries),
                "title": d.get("title", "Playlist")}
    return {
        "kind": "single", "count": 1,
        "title": d.get("title", "Track"),
        "duration": _dur(d.get("duration")),
        "channel": d.get("uploader") or d.get("channel", ""),
    }


async def _fetch_oembed_fallback(url: str) -> dict | None:
    """YouTube public oEmbed API — works even when server IP is blocked for downloads."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.youtube.com/oembed",
                                 params={"url": url, "format": "json"})
            if r.status_code != 200:
                return None
            d = r.json()
        return {
            "kind": "single", "count": 1,
            "title": d.get("title", "YouTube Video"),
            "channel": d.get("author_name", ""),
            "duration": "",
            "blocked": True,
        }
    except Exception as e:
        logger.warning("oEmbed fallback also failed for %s: %s", url, e)
        return None


_RE_SONGS = re.compile(r"(\d+)\s+Song", re.I)


async def _fetch_spotify_info(url: str) -> dict | None:
    """Quick Spotify metadata via the public oembed endpoint (no auth needed)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://open.spotify.com/oembed", params={"url": url})
            if r.status_code != 200:
                return None
            d = r.json()
    except Exception as e:
        logger.warning("Spotify oembed failed: %s", e)
        return None

    title = d.get("title", "")
    desc  = d.get("description", "")  # e.g. "50 Songs. 3 hours, 44 minutes."
    m     = _RE_SONGS.search(desc)
    count = int(m.group(1)) if m else None
    return {"title": title, "count": count, "description": desc}


# ── Confirm UI ────────────────────────────────────────────────────────────────

def _build_confirm(url: str, platform: str, info: dict | None) -> tuple[str, InlineKeyboardMarkup, str]:
    """
    Returns (message_text, keyboard, token).
    Also stores context in _pending[token].
    """
    _clean_pending()
    token = uuid.uuid4().hex[:16]
    is_playlist = False

    if platform == "spotify":
        m    = _RE_SPOTIFY.search(url)
        kind = m.group(1) if m else "track"
        icons  = {"track": "🎵", "album": "💿", "playlist": "📋", "artist": "🎤"}
        labels = {"track": "Track", "album": "Album", "playlist": "Playlist", "artist": "Artist"}
        icon  = icons.get(kind, "🎵")
        label = labels.get(kind, "Track")
        if info and info.get("title"):
            title_line = f"{icon} {info['title']}"
            if info.get("count"):
                title_line += f"\n🎶 {info['count']} tracks"
        else:
            title_line = f"{icon} Spotify {label}"
        text = f"{title_line}\n\nDownload?"
        kb   = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
        ]])
        is_playlist = kind in ("album", "playlist", "artist")

    elif platform == "youtube":
        if info is None:
            text = "▶️ YouTube\n\nDownload?"
            kb   = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
                InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
            ]])
        elif info["kind"] == "playlist":
            is_playlist = True
            text = (f"📋 {info['title']}\n"
                    f"🎵 {info['count']} videos\n\n"
                    f"Download all {info['count']} tracks?")
            kb   = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Download all", callback_data=f"dl:{token}:best"),
                InlineKeyboardButton("❌ Cancel",        callback_data=f"no:{token}"),
            ]])
        else:
            ch      = f"\n👤 {info['channel']}" if info.get("channel") else ""
            dur     = f"  ⏱ {info['duration']}"  if info.get("duration") else ""
            blocked = info.get("blocked", False)
            warn    = "\n\n⚠️ Server IP may be blocked by YouTube.\nDownload might fail — try anyway?" if blocked else "\n\nChoose audio quality:"
            text = f"▶️ {info['title']}{ch}{dur}{warn}"
            kb   = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔊 Best (320k)",   callback_data=f"dl:{token}:best"),
                 InlineKeyboardButton("🎧 Medium (128k)", callback_data=f"dl:{token}:mid"),
                 InlineKeyboardButton("🔈 Low (64k)",     callback_data=f"dl:{token}:low")],
                [InlineKeyboardButton("❌ Cancel",          callback_data=f"no:{token}")],
            ])

    else:  # soundcloud
        if info is None:
            text = "🎵 SoundCloud\n\nDownload?"
        elif info["kind"] == "playlist":
            is_playlist = True
            text = (f"🎵 {info['title']}\n"
                    f"🎶 {info['count']} tracks\n\n"
                    f"Download all {info['count']} tracks?")
        else:
            ch  = f"\n👤 {info['channel']}" if info.get("channel") else ""
            dur = f"  ⏱ {info['duration']}"  if info.get("duration") else ""
            text = f"🎵 {info['title']}{ch}{dur}\n\nDownload?"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
        ]])

    _pending[token] = {
        "url": url,
        "platform": platform,
        "is_playlist": is_playlist,
        "expires": time.time() + CONFIRM_TTL,
    }
    return text, kb, token


# ── Download executors ────────────────────────────────────────────────────────

async def _spotdl_download(url: str, out_dir: Path, msg) -> list[Path]:
    """Download via spotdl — handles Spotify natively with full parallelism."""
    cookie_args = ["--cookie-file", _COOKIES] if os.path.isfile(_COOKIES) else []
    cmd = [
        _SPOTDL, "download", url,
        "--output", str(out_dir / "{title}"),
        "--overwrite", "force",
        "--simple-tui",
    ] + cookie_args + _spotdl_proxy_args()
    logger.info("spotdl cmd: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    done = total = 0
    last_edit = asyncio.get_event_loop().time()

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        logger.info("spotdl: %s", line)
        m = _RE_SPOTPROG.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            now = asyncio.get_event_loop().time()
            if now - last_edit >= 2:
                if total > 1:
                    pct  = int(done * 100 / total)
                    text = f"📥 Track {done}/{total}\n{_bar(pct)} {pct}%"
                else:
                    text = "📥 Downloading..."
                try:
                    await msg.edit_text(text)
                except Exception:
                    pass
                last_edit = now

    await proc.wait()
    return sorted(p for p in out_dir.rglob("*.mp3") if p.is_file())


async def _spotdl_save_ytdlp(url: str, out_dir: Path, msg) -> list[Path]:
    """Fallback for Spotify: resolve metadata via spotdl save, then yt-dlp per track."""
    save_file = out_dir / "meta.spotdl"
    out_dir.mkdir(parents=True, exist_ok=True)
    await msg.edit_text("🔍 Fetching track list from Spotify...")
    out, _ = await _run(
        [_SPOTDL, "save", url, "--save-file", str(save_file)], timeout=180)
    logger.info("spotdl save: %s", out[:300])
    if not save_file.exists():
        return []
    try:
        data  = json.loads(save_file.read_text())
        songs = data if isinstance(data, list) else [data]
    except Exception:
        return []

    total = len(songs)
    files: list[Path] = []
    for i, song in enumerate(songs, 1):
        name   = song.get("name", "")
        artist = song.get("artist", "")
        title  = f"{artist} - {name}" if artist else name
        tdir   = out_dir / f"t{i}"
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            await msg.edit_text(f"📥 Track {i}/{total}\n{title[:60]}")
        except Exception:
            pass
        new, _ = await _ytdlp_download(f"ytsearch1:{title}", tdir, msg)
        files.extend(new)

    return files


_RE_AUTH_ERR = re.compile(r"Sign in to confirm|LOGIN_REQUIRED|bot detection", re.I)
_RE_DRM_ERR  = re.compile(r"DRM protected", re.I)
_RE_GEO_ERR  = re.compile(r"not available in your country|geo.?restricted", re.I)


async def _ytdlp_download(url: str, out_dir: Path, msg,
                           quality: str = "best",
                           is_playlist: bool = False) -> tuple[list[Path], str | None]:
    """Download via yt-dlp. Returns (files, error_type) where error_type is
    'auth', 'drm', 'geo', or None for success."""
    aq       = {"best": "0", "mid": "5", "low": "9"}.get(quality, "0")
    out_tmpl = str(out_dir / (
        "%(playlist_index)02d - %(title)s.%(ext)s" if is_playlist
        else "%(title)s.%(ext)s"))
    no_pl = [] if is_playlist else ["--no-playlist"]

    cmd = [_YTDLP] + _cookie_args() + no_pl + [
        "--format", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3",
        "--audio-quality", aq,
        "--embed-thumbnail", "--add-metadata",
        "--newline",
        "--output", out_tmpl,
        url,
    ]
    logger.info("yt-dlp cmd: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    cur = tot = 0
    error_type: str | None = None
    last_edit = asyncio.get_event_loop().time()

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        logger.info("yt-dlp: %s", line)

        if _RE_AUTH_ERR.search(line):
            error_type = "auth"
        elif _RE_DRM_ERR.search(line):
            error_type = "drm"
        elif _RE_GEO_ERR.search(line):
            error_type = "geo"

        mi = _RE_ITEM.search(line)
        if mi:
            cur, tot = int(mi.group(1)), int(mi.group(2))

        mp = _RE_PCT.search(line)
        if mp:
            pct = min(int(float(mp.group(1))), 100)
            now = asyncio.get_event_loop().time()
            if now - last_edit >= 3:
                text = (f"📥 Track {cur}/{tot}\n{_bar(pct)} {pct}%"
                        if tot > 1 else f"📥 Downloading...\n{_bar(pct)} {pct}%")
                try:
                    await msg.edit_text(text)
                except Exception:
                    pass
                last_edit = now

    await proc.wait()
    files = sorted(p for p in out_dir.rglob("*.mp3") if p.is_file())
    return files, (None if files else error_type)


# ── Upload helper ─────────────────────────────────────────────────────────────

async def _send_audio(bot, chat_id: int, f: Path) -> bool:
    """Upload one mp3 to Telegram. Returns True on success."""
    size = f.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.warning("Skipping %s: %d MB > limit", f.name, size >> 20)
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ {f.stem[:60]} is {size >> 20} MB — too large for Telegram, skipped.")
        except Exception:
            pass
        return False
    try:
        with open(f, "rb") as fh:
            await bot.send_audio(
                chat_id=chat_id, audio=fh,
                title=f.stem[:64], filename=f.name,
                read_timeout=120, write_timeout=120)
        return True
    except Exception as e:
        logger.error("Upload failed %s: %s", f.name, e)
        return False
    finally:
        try:
            f.unlink()
        except OSError:
            pass


# ── Main download orchestrator ────────────────────────────────────────────────

async def _run_download(bot, chat_id: int, msg, ctx: dict) -> None:
    global _active
    url         = ctx["url"]
    platform    = ctx["platform"]
    quality     = ctx.get("quality", "best")
    is_playlist = ctx.get("is_playlist", False)

    if _active >= MAX_CONCURRENT:
        try:
            await msg.edit_text(
                f"⏳ Queued — {_active} download(s) in progress. "
                f"Your request will start automatically, please wait...")
        except Exception:
            pass

    async with _sem:
        _active += 1
        tmpdir = tempfile.mkdtemp(prefix="bot_")
        try:
            out_dir = Path(tmpdir)
            try:
                await msg.edit_text("⚙️ Starting download...")
            except Exception:
                pass

            if platform == "spotify":
                (out_dir / "direct").mkdir(parents=True, exist_ok=True)
                files = await _spotdl_download(url, out_dir / "direct", msg)
                if not files:
                    logger.info("spotdl direct returned no files — trying save+yt-dlp fallback")
                    files = await _spotdl_save_ytdlp(url, out_dir / "fallback", msg)
                err = None if files else "auth"
            else:
                out_dir.mkdir(exist_ok=True)
                files, err = await _ytdlp_download(
                    url, out_dir, msg, quality=quality, is_playlist=is_playlist)

            if not files:
                if err == "drm":
                    await msg.edit_text(
                        "🔒 This content is DRM-protected and cannot be downloaded.")
                elif err == "auth":
                    await msg.edit_text(
                        "🔑 YouTube is blocking this server.\n\n"
                        "Your cookies have expired or the server IP is flagged.\n"
                        "To fix: export fresh cookies from your browser (logged into YouTube) "
                        "as cookies.txt and send that file to this chat — "
                        "the bot will update automatically.")
                elif err == "geo":
                    await msg.edit_text(
                        "🌍 This content is not available in this server's region.")
                else:
                    await msg.edit_text(
                        "❌ Nothing downloaded. The content may not be available.")
                return

            total = len(files)
            try:
                await msg.edit_text(
                    f"📤 Uploading {total} track{'s' if total > 1 else ''}...")
            except Exception:
                pass

            sent = 0
            for f in files:
                if await _send_audio(bot, chat_id, f):
                    sent += 1
                await asyncio.sleep(0.4)  # avoid Telegram flood limits

            if sent == 0:
                await msg.edit_text("❌ All uploads failed.")
            elif sent == total:
                await msg.edit_text(
                    f"✅ Done — {sent} track{'s' if sent > 1 else ''} sent.")
            else:
                skipped = total - sent
                await msg.edit_text(
                    f"✅ Done — {sent}/{total} tracks sent "
                    f"({skipped} skipped, too large or upload error).")

        except Exception as e:
            logger.exception("Download task error")
            try:
                await msg.edit_text(f"❌ Error: {e}")
            except Exception:
                pass
        finally:
            _active -= 1
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, _) -> None:
    await update.message.reply_text(
        "🎵 Music Downloader Bot\n\n"
        "Send me a link and I'll show you what I found — then you decide whether to download.\n\n"
        "Supported:\n"
        "• Spotify — track, album, playlist, artist\n"
        "• YouTube — video, playlist, YouTube Music\n"
        "• SoundCloud — track, set\n\n"
        "For YouTube videos you can choose audio quality (Best / Medium / Low).\n\n"
        "If YouTube downloads fail, export your browser cookies from youtube.com "
        "(use the 'Get cookies.txt LOCALLY' extension) and send the .txt file here — "
        "the bot updates automatically."
    )


async def handle_message(update: Update, context) -> None:
    text = update.message.text.strip()

    is_sp = bool(_RE_SPOTIFY.search(text))
    is_yt = bool(_RE_YOUTUBE.search(text))
    is_sc = bool(_RE_SOUNDCLOUD.search(text))

    if not (is_sp or is_yt or is_sc):
        await update.message.reply_text(
            "Please send a Spotify, YouTube, or SoundCloud URL.")
        return

    msg = await update.message.reply_text("🔍 Looking up...")

    platform = "spotify" if is_sp else ("youtube" if is_yt else "soundcloud")
    info = None
    if is_sp:
        info = await _fetch_spotify_info(text)  # best-effort, None is fine
    else:
        info = await _fetch_yt_sc_info(text)
        if info is None:
            logger.warning("Info fetch returned None for %s — showing generic confirm", text)
            # Fall through with info=None; _build_confirm handles it gracefully

    confirm_text, kb, _ = _build_confirm(text, platform, info)
    await msg.edit_text(confirm_text, reply_markup=kb)


async def handle_callback(update: Update, context) -> None:
    query = update.callback_query
    await query.answer()

    parts   = query.data.split(":")
    action  = parts[0]
    token   = parts[1]
    quality = parts[2] if len(parts) > 2 else "best"

    ctx = _pending.pop(token, None)
    if ctx is None or time.time() > ctx["expires"]:
        await query.edit_message_text(
            "⌛ This request expired. Please send the link again.")
        return

    if action == "no":
        await query.edit_message_text("❌ Download cancelled.")
        return

    ctx["quality"] = quality
    msg      = query.message
    chat_id  = query.message.chat_id
    asyncio.create_task(_run_download(context.bot, chat_id, msg, ctx))


async def handle_cookies_file(update: Update, context) -> None:
    """Accept a cookies.txt file upload and save it for YouTube auth."""
    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text(
            "Please send a .txt file (Netscape format cookies exported from your browser).")
        return
    try:
        f = await context.bot.get_file(doc.file_id)
        content = bytes()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f.file_path)
            r.raise_for_status()
            content = r.content
        text = content.decode(errors="replace")
        if "youtube.com" not in text.lower() and "# Netscape HTTP Cookie" not in text:
            await update.message.reply_text(
                "⚠️ This doesn't look like a YouTube cookies file. "
                "Export it from youtube.com using a browser extension like 'Get cookies.txt LOCALLY'.")
            return
        with open(_COOKIES, "wb") as fh:
            fh.write(content)
        logger.info("cookies.txt updated from Telegram upload (%d bytes)", len(content))
        await update.message.reply_text(
            "✅ cookies.txt updated! YouTube downloads should work now.\n"
            "Try sending a YouTube or Spotify link.")
    except Exception as e:
        logger.error("Cookie upload failed: %s", e)
        await update.message.reply_text(f"❌ Failed to save cookies: {e}")


async def error_handler(update, context) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=True)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")
    app = (Application.builder()
           .token(TOKEN)
           .concurrent_updates(True)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_cookies_file))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    logger.info("Bot running — MAX_CONCURRENT=%d", MAX_CONCURRENT)
    app.run_polling()


if __name__ == "__main__":
    main()
