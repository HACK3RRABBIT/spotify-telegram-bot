#!/usr/bin/env python3
"""
Auto-refresh YouTube cookies using a headless Playwright browser.

Two modes:
  1. EXISTING COOKIES (preferred): Load current cookies.txt into a headless
     browser, navigate to YouTube (already logged in), export refreshed cookies.
     No credentials needed — user only sends cookies.txt ONCE ever.

  2. CREDENTIAL LOGIN (fallback): If no cookies exist, log in with
     YOUTUBE_EMAIL + YOUTUBE_PASSWORD from .env to get initial cookies.

Run manually:  python refresh_cookies.py
Auto-triggered by bot when cookies are missing or >6 days old.
"""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BOT_DIR   = Path(__file__).parent
_COOKIES   = _BOT_DIR / "cookies.txt"
_LOCK_FILE = _BOT_DIR / ".cookie_refresh.lock"
_LOG       = logging.getLogger("cookie_refresh")

YOUTUBE_EMAIL    = os.environ.get("YOUTUBE_EMAIL", "").strip()
YOUTUBE_PASSWORD = os.environ.get("YOUTUBE_PASSWORD", "").strip()

COOKIE_MAX_AGE_SECS = 6 * 24 * 3600  # refresh every 6 days


def cookies_need_refresh() -> bool:
    if not _COOKIES.exists():
        return True
    return time.time() - _COOKIES.stat().st_mtime > COOKIE_MAX_AGE_SECS


def _parse_netscape_cookies(text: str) -> list[dict]:
    """Parse Netscape cookies.txt into Playwright cookie dicts."""
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        try:
            exp = int(expires)
        except ValueError:
            exp = int(time.time()) + 365 * 24 * 3600
        cookies.append({
            "domain":  domain,
            "path":    path,
            "secure":  secure.upper() == "TRUE",
            "expires": exp,
            "name":    name,
            "value":   value,
            "sameSite": "None",
        })
    return cookies


def _write_netscape(cookies: list[dict], path: Path) -> None:
    """Write Playwright cookie dicts as Netscape cookies.txt."""
    lines = ["# Netscape HTTP Cookie File\n"]
    for c in cookies:
        domain  = c.get("domain", "")
        flag    = "TRUE" if domain.startswith(".") else "FALSE"
        path_   = c.get("path", "/")
        secure  = "TRUE" if c.get("secure", False) else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = int(time.time()) + 365 * 24 * 3600
        name  = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\t{flag}\t{path_}\t{secure}\t{expires}\t{name}\t{value}\n")
    path.write_text("".join(lines))


async def _refresh_with_existing_cookies() -> bool:
    """
    Load current cookies into headless browser, visit YouTube to refresh session,
    then export fresh cookies. No credentials needed.
    """
    _LOG.info("Refreshing cookies using existing session...")
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        _LOG.error("playwright not installed")
        return False

    existing = _parse_netscape_cookies(_COOKIES.read_text(errors="replace"))
    if not existing:
        _LOG.warning("Existing cookies.txt is empty or malformed")
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-extensions", "--mute-audio"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        # Load existing cookies into the browser context
        try:
            await ctx.add_cookies(existing)
        except Exception as e:
            _LOG.warning("Some cookies rejected: %s", e)

        page = await ctx.new_page()
        success = False
        try:
            # Visit YouTube — if session is still valid, we stay logged in
            _LOG.info("Navigating to YouTube with existing session...")
            await page.goto("https://www.youtube.com", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            # Check if we're still logged in (avatar appears in top-right)
            is_logged_in = await page.locator('button[aria-label*="account"], #avatar-btn').count() > 0
            _LOG.info("Session check: logged_in=%s url=%s", is_logged_in, page.url)

            # Export refreshed cookies regardless (they'll have updated expiry)
            cookies = await ctx.cookies(["https://youtube.com", "https://www.youtube.com",
                                         "https://music.youtube.com"])
            if cookies:
                _write_netscape(cookies, _COOKIES)
                _LOG.info("Refreshed %d cookies (logged_in=%s)", len(cookies), is_logged_in)
                success = is_logged_in
            else:
                _LOG.warning("No cookies returned after navigation")

        except PWTimeout:
            _LOG.error("Timeout during cookie refresh")
        except Exception as e:
            _LOG.error("Cookie refresh error: %s", e)
        finally:
            await browser.close()

    return success


async def _refresh_with_credentials() -> bool:
    """Login with YOUTUBE_EMAIL/PASSWORD to get fresh cookies (first-time setup)."""
    if not YOUTUBE_EMAIL or not YOUTUBE_PASSWORD:
        _LOG.warning("YOUTUBE_EMAIL / YOUTUBE_PASSWORD not set — cannot do credential login")
        return False

    _LOG.info("Logging in with credentials for %s", YOUTUBE_EMAIL)
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        _LOG.error("playwright not installed")
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-extensions", "--mute-audio"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await ctx.new_page()
        success = False
        try:
            await page.goto("https://accounts.google.com/signin/v2/identifier", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)

            email_input = page.locator('input[type="email"]')
            await email_input.wait_for(state="visible", timeout=10000)
            await email_input.fill(YOUTUBE_EMAIL)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)

            pw_input = page.locator('input[type="password"]')
            await pw_input.wait_for(state="visible", timeout=10000)
            await pw_input.fill(YOUTUBE_PASSWORD)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)

            if "challenge" in page.url or "signin/v2/challenge" in page.url:
                _LOG.error("Google login challenge (2FA/CAPTCHA) — use a no-2FA account")
                return False

            await page.goto("https://www.youtube.com", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            cookies = await ctx.cookies(["https://youtube.com", "https://www.youtube.com",
                                         "https://music.youtube.com"])
            if cookies:
                _write_netscape(cookies, _COOKIES)
                _LOG.info("Credential login: saved %d cookies", len(cookies))
                success = True
        except PWTimeout:
            _LOG.error("Timeout during credential login")
        except Exception as e:
            _LOG.error("Credential login error: %s", e)
        finally:
            await browser.close()

    return success


async def refresh() -> bool:
    """Main entry: try session refresh first, credential login as fallback."""
    # Prevent concurrent refreshes
    if _LOCK_FILE.exists() and time.time() - _LOCK_FILE.stat().st_mtime < 300:
        _LOG.info("Refresh already in progress")
        return False
    _LOCK_FILE.touch()

    try:
        # Mode 1: refresh existing session (preferred — no credentials needed)
        if _COOKIES.exists():
            ok = await _refresh_with_existing_cookies()
            if ok:
                return True
            _LOG.info("Session refresh failed (cookies expired) — trying credential login")

        # Mode 2: credential login (first time, or after full session expiry)
        return await _refresh_with_credentials()
    finally:
        _LOCK_FILE.unlink(missing_ok=True)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not cookies_need_refresh():
        age_h = (time.time() - _COOKIES.stat().st_mtime) / 3600
        print(f"Cookies are fresh ({age_h:.1f}h old) — no refresh needed.")
        return
    ok = await refresh()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
