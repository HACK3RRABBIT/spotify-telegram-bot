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
# On a 1-core/1GB VPS keep this at 1; raise on beefier machines
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "1"))
MAX_FILE_BYTES = 49 << 20  # 49 MB — Telegram bot upload limit
CONFIRM_TTL    = 300        # seconds a confirm button stays active

# Audio format sent to users.
# "m4a"  — fastest: no re-encoding, copies YouTube's AAC stream directly
# "mp3"  — universal but requires ffmpeg CPU re-encode (slower on 1-core VPS)
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "m4a").strip().lower()

YTDLP_PROXY  = os.environ.get("YTDLP_PROXY", "").strip()
SPOTDL_PROXY = os.environ.get("SPOTDL_PROXY", "").strip()

YOUTUBE_EMAIL    = os.environ.get("YOUTUBE_EMAIL", "").strip()
YOUTUBE_PASSWORD = os.environ.get("YOUTUBE_PASSWORD", "").strip()

# Auto-refresh cookies when they're older than this (6 days)
_COOKIE_MAX_AGE = 6 * 24 * 3600
_cookie_refresh_lock = asyncio.Lock()
_cookie_refresh_task: asyncio.Task | None = None


# ── Binary / path resolution ──────────────────────────────────────────────────
def _find_bin(name: str) -> str:
    p = os.path.join(os.path.dirname(sys.executable), name)
    return p if os.path.isfile(p) else name

_SPOTDL  = _find_bin("spotdl")
_YTDLP   = _find_bin("yt-dlp")
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_COOKIES = os.path.join(_BOT_DIR, "cookies.txt")


# ── URL patterns ──────────────────────────────────────────────────────────────
_RE_SPOTIFY     = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|artist)/\S+", re.I)
_RE_YT_MUSIC    = re.compile(
    r"https?://music\.youtube\.com/\S+", re.I)
_RE_YOUTUBE     = re.compile(
    r"https?://(?:(?:www\.)?youtube\.com|youtu\.be)/\S+", re.I)
_RE_SOUNDCLOUD  = re.compile(
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
_RE_PCT      = re.compile(r"\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+/s)", re.I)
_RE_PCT_ONLY = re.compile(r"\[download\]\s+([\d.]+)%")
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


_BGUTIL_URL = os.environ.get("BGUTIL_URL", "http://127.0.0.1:4416").strip()

def _bgutil_args() -> list[str]:
    """Add bgutil PO-token server args if the server is reachable."""
    try:
        import urllib.request
        urllib.request.urlopen(f"{_BGUTIL_URL}/health", timeout=1)
        return ["--extractor-args", f"youtube:getpot_bgutil_baseurl={_BGUTIL_URL}"]
    except Exception:
        return []


def _cookie_args() -> list[str]:
    args = []
    if os.path.isfile(_COOKIES):
        args += ["--cookies", _COOKIES]
    if YTDLP_PROXY:
        args += ["--proxy", YTDLP_PROXY]
    args += _bgutil_args()
    return args


def _spotdl_proxy_args() -> list[str]:
    return ["--proxy", SPOTDL_PROXY] if SPOTDL_PROXY else []


def _cookies_age() -> float:
    """Returns age of cookies.txt in seconds, or infinity if missing."""
    if not os.path.isfile(_COOKIES):
        return float("inf")
    return time.time() - os.path.getmtime(_COOKIES)


async def _auto_refresh_cookies() -> None:
    """Refresh YouTube cookies in the background using refresh_cookies.py."""
    global _cookie_refresh_task
    async with _cookie_refresh_lock:
        if _cookies_age() < _COOKIE_MAX_AGE:
            return
        if not YOUTUBE_EMAIL or not YOUTUBE_PASSWORD:
            logger.info("No YOUTUBE_EMAIL set — skipping auto cookie refresh")
            return

        logger.info("Cookies are stale or missing — starting auto-refresh...")
        try:
            import refresh_cookies  # noqa: PLC0415
            ok = await refresh_cookies.refresh()
            if ok:
                logger.info("Cookie refresh succeeded")
            else:
                logger.warning("Cookie refresh failed — downloads may be limited")
        except Exception as e:
            logger.error("Cookie refresh error: %s", e)


# yt-dlp YouTube client strategies.
# Key rule: mobile clients (android_vr, ios, android) IGNORE --cookies entirely.
# So we never mix cookies with mobile clients.
#
# Each entry is (client_args, use_cookies).
# Download strategy (tried in order, first to produce a file wins).
#
# curl-cffi impersonation (metube approach): spoofs Chrome's TLS fingerprint
# at the network level — most effective against bot detection.
# android_vr: YouTube's TV API endpoint, no login required, fast fallback.
# web+cookies+deno: only useful for age-restricted content with valid cookies.
#
# NOTE: mobile clients (android_vr, ios, android) silently skip --cookies,
# so we never pass cookies to them.
_YT_STRATEGIES = [
    # 1. web + cookies + Deno — handles age-restricted, logged-in content
    (["--extractor-args", "youtube:player_client=web", "--js-runtimes", "deno"], True),
    # 2. Chrome impersonation via curl-cffi (metube-style, best bot bypass)
    (["--impersonate", "chrome"], False),
    # 3. android_vr API — no cookies, no JS runtime needed, reliable on WARP
    (["--extractor-args", "youtube:player_client=android_vr"], False),
    # 4. mweb fallback
    (["--extractor-args", "youtube:player_client=mweb"], False),
]


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
    """Fetch title / track list from YouTube, YouTube Music or SoundCloud."""
    if "soundcloud.com" in url.lower():
        return await _fetch_sc_info(url)
    is_yt_music = "music.youtube.com" in url.lower()
    return await _fetch_yt_info(url, is_yt_music=is_yt_music)


async def _fetch_sc_info(url: str) -> dict | None:
    """SoundCloud: use yt-dlp --print to get real track titles (flat-playlist lacks them)."""
    SEP = "|||"
    cmd = [_YTDLP] + _cookie_args() + [
        "--no-warnings",
        "--print", f"%(title)s{SEP}%(uploader)s{SEP}%(duration)s",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=40)
    except asyncio.TimeoutError:
        proc.kill()
        return None

    lines = [l for l in out_bytes.decode(errors="replace").splitlines() if l.strip()]
    if not lines:
        return None

    if len(lines) == 1:
        parts = lines[0].split(SEP)
        return {
            "kind": "single", "count": 1,
            "title":    parts[0] if parts[0] != "NA" else "Track",
            "channel":  parts[1] if len(parts) > 1 and parts[1] != "NA" else "",
            "duration": _dur(parts[2]) if len(parts) > 2 and parts[2] != "NA" else "?",
        }

    # Playlist — first line is the playlist itself, rest are tracks
    # (yt-dlp prints one line per track when given a playlist URL)
    tracks = []
    playlist_title = ""
    playlist_channel = ""
    for line in lines:
        parts = line.split(SEP)
        title   = parts[0] if parts[0] != "NA" else ""
        channel = parts[1] if len(parts) > 1 and parts[1] != "NA" else ""
        dur     = _dur(parts[2]) if len(parts) > 2 and parts[2] != "NA" else "?"
        if not playlist_title:
            playlist_title   = title
            playlist_channel = channel
        tracks.append({"title": title, "uploader": channel, "duration": dur})

    return {
        "kind": "playlist", "count": len(tracks),
        "title":   playlist_title or "SoundCloud Playlist",
        "channel": playlist_channel,
        "tracks":  tracks,
    }


async def _oembed_yt(url: str) -> dict | None:
    """YouTube oEmbed — instant, no yt-dlp, works even when IP is blocked."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.youtube.com/oembed",
                                 params={"url": url, "format": "json"})
            if r.status_code != 200:
                return None
            d = r.json()
        return {
            "kind": "single", "count": 1,
            "title":   d.get("title", "YouTube Video"),
            "channel": d.get("author_name", ""),
            "duration": "",
        }
    except Exception:
        return None


async def _fetch_yt_info(url: str, is_yt_music: bool = False) -> dict | None:
    """YouTube: oEmbed first (instant), then yt-dlp for playlists/duration."""
    # For a single video: oEmbed is instant and reliable
    is_playlist_url = ("list=" in url or "/playlist" in url)

    if not is_playlist_url:
        info = await _oembed_yt(url)
        if info:
            info["is_yt_music"] = is_yt_music
            return info

    # For playlists or if oEmbed failed: use yt-dlp with short timeout
    proc = await asyncio.create_subprocess_exec(
        *([_YTDLP] + _cookie_args() + [
            "--flat-playlist", "-J", "--no-warnings",
            "--extractor-args", "youtube:player_client=android_vr",
            url,
        ]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        # Fall back to oEmbed for single video
        info = await _oembed_yt(url)
        if info:
            info["is_yt_music"] = is_yt_music
        return info

    try:
        d = json.loads(out_bytes.decode(errors="replace").strip())
    except Exception:
        info = await _oembed_yt(url)
        if info:
            info["is_yt_music"] = is_yt_music
        return info

    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        tracks = [
            {
                "title":    e.get("title", ""),
                "uploader": e.get("uploader") or e.get("channel", ""),
                "duration": _dur(e.get("duration")),
            }
            for e in entries
        ]
        return {
            "kind":       "playlist",
            "count":      len(entries),
            "title":      d.get("title", "Playlist"),
            "channel":    d.get("uploader") or d.get("channel", ""),
            "tracks":     tracks,
            "is_yt_music": is_yt_music,
        }
    return {
        "kind":       "single",
        "count":      1,
        "title":      d.get("title", "Track"),
        "duration":   _dur(d.get("duration")),
        "channel":    d.get("uploader") or d.get("channel", ""),
        "is_video":   not is_yt_music,
        "is_yt_music": is_yt_music,
    }


async def _fetch_spotify_info(url: str, msg=None) -> dict | None:
    """
    Fetch Spotify metadata.
    - For single tracks: use fast oEmbed (< 1s).
    - For playlists/albums/artists: run spotdl save to get full track list.
    """
    m = _RE_SPOTIFY.search(url)
    kind = m.group(1) if m else "track"

    # Always get the display title from oEmbed (fast)
    title = None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://open.spotify.com/oembed", params={"url": url})
            if r.status_code == 200:
                title = r.json().get("title", "")
    except Exception:
        pass

    if kind == "track":
        return {"title": title or "Track", "count": 1, "songs": []}

    # For playlist / album / artist — run spotdl save to get track list
    if msg:
        try:
            await msg.edit_text(f"🔍 Fetching track list...")
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as td:
        save_file = Path(td) / "meta.spotdl"
        out, rc = await _run(
            [_SPOTDL, "save", url, "--save-file", str(save_file)],
            timeout=180,
        )
        if not save_file.exists():
            logger.warning("spotdl save produced no file for %s: %s", url, out[:200])
            return {"title": title or kind.capitalize(), "count": None, "songs": []}
        try:
            data  = json.loads(save_file.read_text())
            songs = data if isinstance(data, list) else [data]
        except Exception:
            songs = []

    return {
        "title": title or kind.capitalize(),
        "count": len(songs),
        "songs": songs,   # list of {name, artist, ...}
    }


# ── Confirm UI ────────────────────────────────────────────────────────────────

def _format_track_list(tracks: list, total: int, limit: int = 15) -> str:
    """Return a numbered track list string for playlists. Empty string if no tracks."""
    if not tracks:
        return ""
    lines = []
    for i, t in enumerate(tracks[:limit], 1):
        # Spotify songs have 'name'/'artist'; yt-dlp entries have 'title'/'uploader'
        name     = t.get("name") or t.get("title", "")
        artist   = t.get("artist") or t.get("uploader", "")
        dur      = f"  ⏱{t['duration']}" if t.get("duration") and t["duration"] != "?" else ""
        if artist:
            lines.append(f"{i}. {artist} — {name}{dur}")
        else:
            lines.append(f"{i}. {name}{dur}")
    result = "\n" + "\n".join(lines)
    if total > limit:
        result += f"\n_... and {total - limit} more_"
    return result


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
        icon  = icons.get(kind, "🎵")
        pl_title = (info or {}).get("title") or f"Spotify {kind.capitalize()}"
        songs    = (info or {}).get("songs", [])
        count    = (info or {}).get("count") or len(songs)

        title_line = f"{icon} *{pl_title}*"
        if count:
            title_line += f"\n🎶 {count} track{'s' if count != 1 else ''}"

        # Show track listing for playlists/albums
        track_list = _format_track_list(songs, count or 0) if kind != "track" else ""

        text = f"{title_line}{track_list}\n\nDownload?"
        kb   = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
        ]])
        is_playlist = kind in ("album", "playlist", "artist")

    elif platform == "youtube":
        is_yt_music = (info or {}).get("is_yt_music", False)
        yt_icon = "🎵" if is_yt_music else "🎬"
        if info is None:
            text = f"{yt_icon} YouTube\n\nDownload?"
            kb   = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
                InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
            ]])
        elif info["kind"] == "playlist":
            is_playlist = True
            track_list  = _format_track_list(info.get("tracks", []), info["count"])
            label = "tracks" if is_yt_music else "videos"
            text = (f"📋 *{info['title']}*\n"
                    f"{yt_icon} {info['count']} {label}"
                    f"{track_list}\n\nDownload all {info['count']} {label}?")
            kb   = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Download all", callback_data=f"dl:{token}:best"),
                InlineKeyboardButton("❌ Cancel",        callback_data=f"no:{token}"),
            ]])
        else:
            ch  = f"\n👤 {info['channel']}" if info.get("channel") else ""
            dur = f"  ⏱ {info['duration']}"  if info.get("duration") else ""
            if is_yt_music:
                text = f"🎵 *{info['title']}*{ch}{dur}\n\nChoose audio quality:"
                kb   = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔊 Best",   callback_data=f"dl:{token}:best"),
                     InlineKeyboardButton("🎧 Medium", callback_data=f"dl:{token}:mid"),
                     InlineKeyboardButton("🔈 Low",    callback_data=f"dl:{token}:low")],
                    [InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}")],
                ])
            else:
                text = f"🎬 *{info['title']}*{ch}{dur}\n\nDownload as:"
                kb   = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video",       callback_data=f"dl:{token}:video"),
                     InlineKeyboardButton("🎵 Audio only",  callback_data=f"dl:{token}:best")],
                    [InlineKeyboardButton("❌ Cancel",        callback_data=f"no:{token}")],
                ])

    else:  # soundcloud
        if info is None:
            text = "🎵 SoundCloud\n\nDownload?"
        elif info["kind"] == "playlist":
            is_playlist = True
            ch         = f"\n👤 {info['channel']}" if info.get("channel") else ""
            track_list = _format_track_list(info.get("tracks", []), info["count"])
            text = (f"🎵 *{info['title']}*{ch}\n"
                    f"🎶 {info['count']} tracks"
                    f"{track_list}\n\nDownload all {info['count']} tracks?")
        else:
            ch  = f"\n👤 {info['channel']}" if info.get("channel") else ""
            dur = f"  ⏱ {info['duration']}"  if info.get("duration") else ""
            text = f"🎵 *{info['title']}*{ch}{dur}\n\nDownload?"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Download", callback_data=f"dl:{token}:best"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"no:{token}"),
        ]])

    _pending[token] = {
        "url": url,
        "platform": platform,
        "is_playlist": is_playlist,
        "songs": (info or {}).get("songs", []) if platform == "spotify" else [],
        "expires": time.time() + CONFIRM_TTL,
        "reply_to": None,  # filled in handle_message
    }
    return text, kb, token


# ── Download executors ────────────────────────────────────────────────────────

async def _spotdl_download(url: str, out_dir: Path, msg) -> list[Path]:
    """Download via spotdl — handles Spotify natively with full parallelism."""
    cookie_args = ["--cookie-file", _COOKIES] if os.path.isfile(_COOKIES) else []
    # --threads 2: spotdl default is 4 which saturates a 1-core VPS
    # --format m4a: skip re-encoding, copy AAC stream directly (much faster)
    # Prefer SoundCloud → YouTube Music → YouTube → Piped (avoids YouTube bot detection)
    audio_providers = ["soundcloud", "youtube-music", "youtube", "piped"]
    cmd = [
        _SPOTDL, "download", url,
        "--output", str(out_dir / "{title}"),
        "--overwrite", "force",
        "--simple-tui",
        "--threads", "2",
        "--format", AUDIO_FORMAT,
        "--audio", *audio_providers,
        "--dont-filter-results",
    ] + cookie_args + _spotdl_proxy_args()
    logger.info("spotdl cmd: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    done = total = 0
    speed = ""
    last_edit = asyncio.get_event_loop().time()

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        logger.info("spotdl: %s", line)

        mp = _RE_PCT.search(line)
        if mp:
            speed = mp.group(2)

        m = _RE_SPOTPROG.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            now = asyncio.get_event_loop().time()
            if now - last_edit >= 2:
                spd = f" • {speed}" if speed else ""
                if total > 1:
                    pct  = int(done * 100 / total)
                    text = f"📥 File {done}/{total}{spd}\n{_bar(pct)} {pct}%"
                else:
                    text = f"📥 Downloading{spd}..."
                try:
                    await msg.edit_text(text)
                except Exception:
                    pass
                last_edit = now

    await proc.wait()
    return sorted(p for p in out_dir.rglob(f"*.{AUDIO_FORMAT}") if p.is_file())


async def _spotdl_save_ytdlp(url: str, out_dir: Path, msg, prefetched_songs: list | None = None) -> list[Path]:
    """Fallback for Spotify: resolve metadata via spotdl save, then yt-dlp per track."""
    out_dir.mkdir(parents=True, exist_ok=True)

    if prefetched_songs:
        songs = prefetched_songs
    else:
        save_file = out_dir / "meta.spotdl"
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
        # Try SoundCloud first — no bot detection, no auth needed
        sc, sc_err = await _ytdlp_download(f"scsearch1:{title}", tdir, msg)
        if sc:
            files.extend(sc)
        else:
            # Fall back to YouTube search
            yt, _ = await _ytdlp_download(f"ytsearch1:{title}", tdir, msg)
            files.extend(yt)

    return files


_RE_AUTH_ERR = re.compile(r"Sign in to confirm|LOGIN_REQUIRED|bot detection", re.I)
_RE_DRM_ERR  = re.compile(r"DRM protected", re.I)
_RE_GEO_ERR  = re.compile(r"not available in your country|geo.?restricted", re.I)


async def _ytdlp_download(url: str, out_dir: Path, msg,
                           quality: str = "best",
                           is_playlist: bool = False) -> tuple[list[Path], str | None]:
    """Download via yt-dlp. Returns (files, error_type) where error_type is
    'auth', 'drm', 'geo', or None for success."""
    out_tmpl = str(out_dir / (
        "%(playlist_index)02d - %(title)s.%(ext)s" if is_playlist
        else "%(title)s.%(ext)s"))
    no_pl = [] if is_playlist else ["--no-playlist"]

    if AUDIO_FORMAT == "mp3":
        aq = {"best": "0", "mid": "5", "low": "9"}.get(quality, "0")
        fmt_args = [
            "--format", "bestaudio/best",
            "--extract-audio", "--audio-format", "mp3", "--audio-quality", aq,
        ]
        ext_glob = "*.mp3"
    else:
        q_fmt = {
            "best": f"bestaudio[ext={AUDIO_FORMAT}]/bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "mid":  "bestaudio[abr<=128]/bestaudio/best",
            "low":  "bestaudio[abr<=64]/bestaudio/best",
        }.get(quality, f"bestaudio[ext={AUDIO_FORMAT}]/bestaudio/best")
        fmt_args = ["--format", q_fmt]
        ext_glob = f"*.{AUDIO_FORMAT}"

    has_cookies = os.path.isfile(_COOKIES)
    proxy_args = ["--proxy", YTDLP_PROXY] if YTDLP_PROXY else []
    ignore_err = ["--ignore-errors"] if is_playlist else []
    base_cmd = [_YTDLP] + proxy_args + no_pl + ignore_err + fmt_args + [
        "--add-metadata", "--newline", "--output", out_tmpl,
    ]

    # Try each client strategy in order until one produces a file
    error_type: str | None = None
    for attempt, (client_args, use_cookies) in enumerate(_YT_STRATEGIES):
        cookie_args = ["--cookies", _COOKIES] if (use_cookies and has_cookies) else []
        cmd = base_cmd + cookie_args + client_args + [url]
        logger.info("yt-dlp attempt %d (cookies=%s): %s", attempt + 1, use_cookies, " ".join(cmd))

        if attempt > 0:
            try:
                await msg.edit_text(f"⚠️ Retrying with method {attempt + 1}/4...")
            except Exception:
                pass
            # Clean up any partial files from previous attempt
            for f in out_dir.rglob("*.part"):
                try: f.unlink()
                except OSError: pass

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        cur = tot = 0
        speed = ""
        done_count = 0
        attempt_error: str | None = None
        last_edit = asyncio.get_event_loop().time()

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            logger.info("yt-dlp: %s", line)

            if _RE_AUTH_ERR.search(line):
                attempt_error = "auth"
            elif _RE_DRM_ERR.search(line):
                attempt_error = "drm"
            elif _RE_GEO_ERR.search(line):
                attempt_error = "geo"

            mi = _RE_ITEM.search(line)
            if mi:
                cur, tot = int(mi.group(1)), int(mi.group(2))

            if "[download] 100%" in line and cur > 0:
                done_count = cur

            mp = _RE_PCT.search(line)
            if mp:
                pct = min(int(float(mp.group(1))), 100)
                speed = mp.group(2)
            else:
                mp2 = _RE_PCT_ONLY.search(line)
                if mp2:
                    pct = min(int(float(mp2.group(1))), 100)
                else:
                    pct = None

            if pct is not None:
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 2:
                    spd = f" • {speed}" if speed else ""
                    if tot > 1:
                        text = (f"📥 File {cur}/{tot}{spd}\n"
                                f"{_bar(pct)} {pct}%")
                    else:
                        text = f"📥 Downloading{spd}\n{_bar(pct)} {pct}%"
                    try:
                        await msg.edit_text(text)
                    except Exception:
                        pass
                    last_edit = now

        await proc.wait()

        # Check if we got files — if yes, stop retrying
        files = sorted(p for p in out_dir.rglob(ext_glob) if p.is_file())
        if not files:
            files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.suffix in
                           {".mp3", ".m4a", ".webm", ".opus", ".ogg", ".flac", ".wav"})
        if files:
            return files, None

        error_type = attempt_error
        # DRM / geo errors won't be fixed by retrying with a different client
        if attempt_error in ("drm", "geo"):
            break

    return [], error_type


async def _ytdlp_video_download(url: str, out_dir: Path, msg) -> tuple[list[Path], str | None]:
    """Download a YouTube video (up to 50MB) with audio merged."""
    out_tmpl = str(out_dir / "%(title)s.%(ext)s")
    # Best video+audio up to ~720p merged into mp4; cap size to stay under 50MB
    has_cookies = os.path.isfile(_COOKIES)
    proxy_args = ["--proxy", YTDLP_PROXY] if YTDLP_PROXY else []
    base_cmd = [_YTDLP] + proxy_args + [
        "--format", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--add-metadata",
        "--newline",
        "--output", out_tmpl,
    ]

    for attempt, (client_args, use_cookies) in enumerate(_YT_STRATEGIES):
        cookie_args = ["--cookies", _COOKIES] if (use_cookies and has_cookies) else []
        full_cmd = base_cmd + cookie_args + client_args + [url]
        logger.info("yt-dlp video attempt %d (cookies=%s)", attempt + 1, use_cookies)
        if attempt > 0:
            try: await msg.edit_text(f"⚠️ Retrying video download (method {attempt + 1}/4)...")
            except Exception: pass

        proc = await asyncio.create_subprocess_exec(
            *full_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        attempt_error = None
        speed = ""
        last_edit = asyncio.get_event_loop().time()
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line: continue
            logger.info("yt-dlp video: %s", line)
            if _RE_AUTH_ERR.search(line): attempt_error = "auth"
            elif _RE_DRM_ERR.search(line): attempt_error = "drm"
            elif _RE_GEO_ERR.search(line): attempt_error = "geo"
            mp = _RE_PCT.search(line)
            pct = None
            if mp:
                pct = min(int(float(mp.group(1))), 100)
                speed = mp.group(2)
            else:
                mp2 = _RE_PCT_ONLY.search(line)
                if mp2:
                    pct = min(int(float(mp2.group(1))), 100)
            if pct is not None:
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 2:
                    spd = f" • {speed}" if speed else ""
                    try: await msg.edit_text(f"🎬 Downloading video{spd}\n{_bar(pct)} {pct}%")
                    except Exception: pass
                    last_edit = now

        await proc.wait()

        files = sorted(p for p in out_dir.rglob("*.mp4") if p.is_file())
        if not files:
            files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.suffix.lower()
                           in {".mp4", ".mkv", ".webm", ".mov"})
        if files:
            # Skip files >49MB
            files = [f for f in files if f.stat().st_size <= MAX_FILE_BYTES]
            if files:
                return files, None

        if attempt_error in ("drm", "geo"):
            break

    return [], attempt_error or "auth"


# ── Upload helpers ────────────────────────────────────────────────────────────

async def _send_video(bot, chat_id: int, f: Path, reply_to: int | None = None) -> bool:
    """Upload a video file to Telegram. Returns True on success."""
    size = f.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.warning("Skipping video %s: %d MB", f.name, size >> 20)
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ Video is {size >> 20} MB — too large for Telegram (50 MB limit).",
                reply_to_message_id=reply_to)
        except Exception:
            pass
        return False
    try:
        with open(f, "rb") as fh:
            await bot.send_video(
                chat_id,
                fh,
                caption=f.stem[:200],
                supports_streaming=True,
                reply_to_message_id=reply_to,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )
        return True
    except Exception as e:
        logger.error("send_video failed for %s: %s", f.name, e)
        return False


async def _send_audio(bot, chat_id: int, f: Path, reply_to: int | None = None) -> bool:
    """Upload one mp3 to Telegram. Returns True on success."""
    size = f.stat().st_size
    if size > MAX_FILE_BYTES:
        logger.warning("Skipping %s: %d MB > limit", f.name, size >> 20)
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ {f.stem[:60]} is {size >> 20} MB — too large for Telegram, skipped.",
                reply_to_message_id=reply_to)
        except Exception:
            pass
        return False
    try:
        with open(f, "rb") as fh:
            await bot.send_audio(
                chat_id=chat_id, audio=fh,
                title=f.stem[:64], filename=f.name,
                reply_to_message_id=reply_to,
                read_timeout=300, write_timeout=300, connect_timeout=30)
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
    reply_to    = ctx.get("reply_to")

    if _active >= MAX_CONCURRENT:
        try:
            await msg.edit_text(
                f"⏳ Queued — {_active} download(s) in progress. "
                f"Your request will start automatically, please wait...")
        except Exception:
            pass

    # Auto-refresh cookies before download if stale (non-blocking for other logic)
    if _cookies_age() > _COOKIE_MAX_AGE:
        asyncio.create_task(_auto_refresh_cookies())

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
                    files = await _spotdl_save_ytdlp(
                        url, out_dir / "fallback", msg,
                        prefetched_songs=ctx.get("songs") or None,
                    )
                err = None if files else "auth"
            elif quality == "video":
                out_dir.mkdir(exist_ok=True)
                files, err = await _ytdlp_video_download(url, out_dir, msg)
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
                        "🚫 YouTube blocked this download.\n\n"
                        "This video is age-restricted and YouTube blocks server IPs "
                        "from downloading it — even with valid cookies. "
                        "This is a YouTube network-level restriction on datacenter IPs, "
                        "not a cookies problem.")
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
                is_video_file = f.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}
                if is_video_file:
                    ok = await _send_video(bot, chat_id, f, reply_to=reply_to)
                else:
                    ok = await _send_audio(bot, chat_id, f, reply_to=reply_to)
                if ok:
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

    is_sp  = bool(_RE_SPOTIFY.search(text))
    is_ytm = bool(_RE_YT_MUSIC.search(text))
    is_yt  = bool(_RE_YOUTUBE.search(text)) or is_ytm
    is_sc  = bool(_RE_SOUNDCLOUD.search(text))

    if not (is_sp or is_yt or is_sc):
        await update.message.reply_text(
            "Please send a Spotify, YouTube, YouTube Music, or SoundCloud URL.")
        return

    msg = await update.message.reply_text("🔍 Looking up...")

    platform = "spotify" if is_sp else ("youtube" if is_yt else "soundcloud")
    info = None
    if is_sp:
        info = await _fetch_spotify_info(text, msg=msg)
    else:
        info = await _fetch_yt_sc_info(text)
        if info is None:
            logger.warning("Info fetch returned None for %s — showing generic confirm", text)
            # Fall through with info=None; _build_confirm handles it gracefully

    confirm_text, kb, token = _build_confirm(text, platform, info)
    # Store the original message ID so uploads can reply to it
    if token in _pending:
        _pending[token]["reply_to"] = update.message.message_id
    await msg.edit_text(confirm_text, reply_markup=kb, parse_mode="Markdown")


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
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".txt"):
        # Silently ignore non-txt documents (photos, audio, etc.)
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        import io
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue()
        text = content.decode(errors="replace")
        if "youtube.com" not in text.lower() and "netscape http cookie" not in text.lower():
            await update.message.reply_text(
                "⚠️ This doesn't look like a YouTube cookies file.\n"
                "Export from youtube.com using 'Get cookies.txt LOCALLY' browser extension.")
            return
        with open(_COOKIES, "wb") as fh:
            fh.write(content)
        logger.info("cookies.txt saved (%d bytes, %d lines)", len(content), text.count("\n"))
        logger.info("cookies.txt updated from Telegram upload (%d bytes)", len(content))

        # Quick validation: try fetching a known public video with these cookies
        await update.message.reply_text("⏳ Validating cookies...")
        val_out, val_rc = await _run([
            _YTDLP,
            "--cookies", _COOKIES,
            "--proxy", YTDLP_PROXY,
            "--extractor-args", "youtube:player_client=web",
            "--js-runtimes", "deno",
            "--no-download", "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ], timeout=30)

        if "rick astley" in val_out.lower() or val_rc == 0:
            await update.message.reply_text(
                "✅ Cookies valid! YouTube downloads are now fully unlocked.\n"
                "Age-restricted and logged-in content will now work.")
        else:
            # Cookies saved but may be invalid — warn user
            logger.warning("Cookie validation failed: rc=%d out=%s", val_rc, val_out[:200])
            await update.message.reply_text(
                "⚠️ Cookies saved but appear expired or invalid.\n"
                "Normal YouTube videos will still work via WARP.\n"
                "For age-restricted content, export fresh cookies from an active browser session.")
    except Exception as e:
        logger.error("Cookie upload failed: %s", e)
        await update.message.reply_text(f"❌ Failed to save cookies: {e}")


async def error_handler(update, context) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=True)


async def _post_init(app) -> None:
    """Run after bot connects — kick off cookie refresh in background."""
    if YOUTUBE_EMAIL and YOUTUBE_PASSWORD and _cookies_age() > _COOKIE_MAX_AGE:
        logger.info("Scheduling background cookie refresh on startup...")
        asyncio.create_task(_auto_refresh_cookies())


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")
    app = (Application.builder()
           .token(TOKEN)
           .concurrent_updates(True)
           .post_init(_post_init)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Accept any document — handler checks for .txt extension itself
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_file))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    logger.info("Bot running — MAX_CONCURRENT=%d  cookie_email=%s",
                MAX_CONCURRENT, YOUTUBE_EMAIL or "not set")
    app.run_polling()


if __name__ == "__main__":
    main()
