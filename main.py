"""
Dual-bot Telegram + Browser automation system.
  - Userbot:      Telethon user session, iterates all tasks, clicks buttons,
                  injects referral tokens, opens links in Playwright.
  - Control Bot:  Telegram bot (token-based) for the owner to start/stop/monitor.
  - Flask:        Lightweight health server for Railway port-binding.

First-time auth (run locally — requires an interactive terminal):
    python main.py auth

    This prompts for phone → OTP → 2FA, saves a local session file, AND
    prints a SESSION_STRING value.  Set that value as the SESSION_STRING
    environment variable on Railway so the session survives redeployments.
    See replit.md for the full step-by-step guide.

Normal run (Railway uses this via Procfile):
    python main.py
"""

import asyncio
import json
import logging
import os
import random
import re
import threading
import tempfile
import shutil
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    ReplyInlineMarkup,
    KeyboardButtonUrl,
    KeyboardButtonCallback,
    ReplyKeyboardMarkup,
    KeyboardButtonRow,
    KeyboardButton,
)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "config.json")
TARGETS_PATH  = os.path.join(BASE_DIR, "targets.json")
PROXIES_PATH  = os.path.join(BASE_DIR, "proxies.txt")

# File-level lock — guards all reads AND writes to targets.json so that a
# concurrent control-bot write never races with the userbot's load.
_targets_lock = threading.Lock()

# Lock for proxies.txt — shared between the listener and anything else that
# reads the proxy list so concurrent writes are safe.
_proxies_lock = threading.Lock()

# ─── Config / Targets helpers ─────────────────────────────────────────────────

def load_config() -> dict:
    """
    Builds the config dict from environment variables (Railway / production).
    Falls back to config.json values for local development.
    Environment variables always win — they are never overridden by the file.

    Required env vars:
        API_ID, API_HASH, CONTROL_BOT_TOKEN, PERSONAL_TELEGRAM_ID,
        PROXY_HOST, PROXY_PORT, PROXY_USERNAME, PROXY_PASSWORD
    """
    # Try to load config.json as a local-dev fallback (values are placeholders in prod)
    file_cfg: dict = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Skip placeholder strings that start with "${"
        file_cfg = {k: v for k, v in raw.items() if not str(v).startswith("${")}
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    def _get(env_key: str, file_key: str, cast=str, required: bool = True):
        val = os.environ.get(env_key) or file_cfg.get(file_key)
        if not val:
            if required:
                log.error(
                    "Missing required config: set the '%s' environment variable.", env_key
                )
            return None
        try:
            return cast(val)
        except (ValueError, TypeError) as exc:
            log.error("Config '%s' cast error: %s", env_key, exc)
            return None

    # proxy_host may arrive as "host:port" — split it so callers always get a
    # clean hostname.  PROXY_PORT still wins if set explicitly.
    raw_proxy_host = _get("PROXY_HOST", "proxy_host", required=False) or ""
    if ":" in raw_proxy_host:
        _ph_parts = raw_proxy_host.rsplit(":", 1)
        _proxy_host_clean = _ph_parts[0]
        _proxy_port_from_host = int(_ph_parts[1]) if _ph_parts[1].isdigit() else 443
    else:
        _proxy_host_clean = raw_proxy_host
        _proxy_port_from_host = 443

    _explicit_port = _get("PROXY_PORT", "proxy_port", int, required=False)

    cfg = {
        "api_id":                   _get("API_ID",               "api_id",                   int),
        "api_hash":                  _get("API_HASH",              "api_hash"),
        "control_bot_token":         _get("CONTROL_BOT_TOKEN",     "control_bot_token"),
        "your_personal_telegram_id": _get("PERSONAL_TELEGRAM_ID", "your_personal_telegram_id", int),
        "proxy_host":                _proxy_host_clean,
        "proxy_port":                _explicit_port if _explicit_port else _proxy_port_from_host,
        "proxy_username":            _get("PROXY_USERNAME",        "proxy_username",           required=False) or "",
        "proxy_password":            _get("PROXY_PASSWORD",        "proxy_password",           required=False) or "",
    }

    # Surface any fatal misconfigurations immediately at startup
    fatal = [k for k, v in cfg.items() if v is None]
    if fatal:
        raise RuntimeError(
            f"Cannot start — missing required environment variables: "
            + ", ".join(fatal)
        )

    return cfg


def load_targets() -> dict:
    """Thread-safe read of targets.json with fallback on parse error."""
    with _targets_lock:
        try:
            with open(TARGETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Guarantee the key exists and is a list
            if not isinstance(data.get("tasks"), list):
                data["tasks"] = []
            return data
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            log.error("targets.json read error (%s) — returning empty task list.", exc)
            return {"tasks": []}


def save_targets(data: dict) -> None:
    """
    Thread-safe, atomic write to targets.json.
    Writes to a sibling temp file first, then renames to prevent partial-write
    corruption if the process is killed mid-write.
    """
    with _targets_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=BASE_DIR, prefix=".targets_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            shutil.move(tmp_path, TARGETS_PATH)
        except Exception:
            # Clean up the temp file if something went wrong
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

# ─── Proxy helpers ────────────────────────────────────────────────────────────

def _get_userbot_session():
    """
    Returns the Telethon session argument for the userbot client.

    - If SESSION_STRING env var is set, uses StringSession (Railway / any
      cloud platform with ephemeral filesystems).
    - Otherwise falls back to the local session file (for local dev after
      running `python main.py auth`).

    To generate SESSION_STRING:
        1. Run `python main.py auth` locally with all env vars set.
        2. Copy the session string that is printed at the end.
        3. Set SESSION_STRING=<that string> in your Railway env vars.
    """
    session_string = os.environ.get("SESSION_STRING", "").strip()
    if session_string:
        log.info("Userbot: using StringSession from SESSION_STRING env var.")
        return StringSession(session_string)
    session_file = os.path.join(BASE_DIR, "userbot_session")
    log.info("Userbot: using file-based session at %s", session_file)
    return session_file


def fresh_session_suffix() -> str:
    """
    Generates a unique random suffix appended to the Asocks proxy username.
    Each call produces a different integer, which signals Asocks to allocate
    a brand-new US residential IP for that session — guaranteeing zero IP reuse
    across cycles.  Formula: <PROXY_USERNAME>-session_<random 6-digit int>
    """
    return f"-session_{random.randint(100000, 999999)}"


def build_telethon_proxy(cfg: dict, suffix: str) -> dict:
    """SOCKS5 proxy dict accepted by Telethon's `proxy=` parameter."""
    return {
        "proxy_type": "socks5",
        "addr": cfg["proxy_host"],
        "port": int(cfg["proxy_port"]),
        "username": cfg["proxy_username"] + suffix,
        "password": cfg["proxy_password"],
        "rdns": True,
    }


def build_requests_proxy(cfg: dict, suffix: str) -> dict:
    """Proxy dict accepted by the `requests` library."""
    user = cfg["proxy_username"] + suffix
    url = (
        f"socks5h://{user}:{cfg['proxy_password']}"
        f"@{cfg['proxy_host']}:{cfg['proxy_port']}"
    )
    return {"http": url, "https": url}


def build_playwright_proxy(cfg: dict, suffix: str) -> dict:
    """Proxy dict accepted by Playwright's `browser.launch(proxy=...)`."""
    return {
        "server": f"socks5://{cfg['proxy_host']}:{cfg['proxy_port']}",
        "username": cfg["proxy_username"] + suffix,
        "password": cfg["proxy_password"],
    }


async def get_current_ip_async(requests_proxy: dict) -> str:
    """
    Non-blocking IP lookup through the given requests-style proxy dict.
    Runs the blocking `requests.get` in a thread pool so it never stalls
    the asyncio event loop.
    """
    def _fetch():
        try:
            r = requests.get(
                "https://api.ipify.org?format=json",
                proxies=requests_proxy,
                timeout=15,
            )
            return r.json().get("ip", "unknown")
        except Exception as exc:
            log.warning("IP check failed: %s", exc)
            return "unavailable"

    return await asyncio.to_thread(_fetch)


# ─── Proxy pool helpers ────────────────────────────────────────────────────────

def load_proxy_pool() -> list[str]:
    """Read proxies.txt and return non-empty, non-comment lines."""
    with _proxies_lock:
        try:
            with open(PROXIES_PATH, "r", encoding="utf-8") as fh:
                return [
                    ln.strip()
                    for ln in fh
                    if ln.strip() and not ln.strip().startswith("#")
                ]
        except FileNotFoundError:
            return []


def parse_proxy_url(url: str) -> dict | None:
    """
    Parse a full proxy URL into a normalised component dict.

    Supports:
      http://1.2.3.4:8080
      socks5://user:pass@1.2.3.4:1080
      socks4://host:port
    Returns None if the URL cannot be parsed or the port is invalid.
    """
    try:
        p = urlparse(url)
        proto = (p.scheme or "http").lower()
        if proto not in ("http", "https", "socks4", "socks5"):
            proto = "http"
        port = p.port
        if not port or not (1 <= port <= 65535):
            return None
        return {
            "proxy_type": proto,
            "host":       p.hostname or "",
            "port":       port,
            "username":   p.username or "",
            "password":   p.password or "",
        }
    except Exception:
        return None


def _pool_telethon(p: dict) -> dict:
    """Build a Telethon proxy dict from a parsed pool entry."""
    d: dict = {
        "proxy_type": p["proxy_type"],
        "addr":       p["host"],
        "port":       p["port"],
        "rdns":       True,
    }
    if p["username"]:
        d["username"] = p["username"]
        d["password"] = p["password"]
    return d


def _pool_requests(p: dict) -> dict:
    """Build a requests proxy dict from a parsed pool entry."""
    scheme = "socks5h" if p["proxy_type"] == "socks5" else p["proxy_type"]
    if p["username"]:
        url = f"{scheme}://{p['username']}:{p['password']}@{p['host']}:{p['port']}"
    else:
        url = f"{scheme}://{p['host']}:{p['port']}"
    return {"http": url, "https": url}


def _pool_playwright(p: dict) -> dict:
    """Build a Playwright proxy dict from a parsed pool entry."""
    d: dict = {"server": f"{p['proxy_type']}://{p['host']}:{p['port']}"}
    if p["username"]:
        d["username"] = p["username"]
        d["password"] = p["password"]
    return d


def resolve_cycle_proxies(cfg: dict, suffix: str) -> tuple[dict, dict, dict, str]:
    """
    Select the proxy set for one automation cycle.

    Priority
    --------
    1. proxies.txt pool  — if the file has entries, one is picked at random.
    2. Asocks fallback   — uses PROXY_* env-var credentials + session suffix.

    Returns
    -------
    (telethon_proxy, requests_proxy, playwright_proxy, label)

    ``label`` is logged at cycle start so you can see which proxy was chosen.
    """
    pool = load_proxy_pool()
    if pool:
        url = random.choice(pool)
        parsed = parse_proxy_url(url)
        if parsed:
            log.info("Proxy pool selected: %s", url)
            return (
                _pool_telethon(parsed),
                _pool_requests(parsed),
                _pool_playwright(parsed),
                f"pool → {url}",
            )
        log.warning("Could not parse pool entry '%s' — falling back to Asocks.", url)

    # Asocks fallback
    log.info("No valid pool proxy — using Asocks session%s.", suffix)
    return (
        build_telethon_proxy(cfg, suffix),
        build_requests_proxy(cfg, suffix),
        build_playwright_proxy(cfg, suffix),
        f"asocks{suffix}",
    )

# ─── Referral / affiliate URL injection ──────────────────────────────────────

def inject_referral(url: str, referral_append_url: str) -> str:
    """
    Merges or substitutes a referral/affiliate token into a target URL.

    Logic:
      - Empty referral → return url unchanged.
      - referral_append_url contains "=" (looks like "?ref=TOKEN&foo=bar"):
          merge its key-value pairs into the URL's query string, overwriting
          any existing values for those keys.
      - Plain token string (no "="):
          look for known referral param names in the URL query string
          (ref, r, start, invite, referral) and replace the first match;
          if none found, append ?ref=<token>.
    """
    if not referral_append_url:
        return url

    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    if "=" in referral_append_url:
        extra = parse_qs(referral_append_url.lstrip("?"), keep_blank_values=True)
        qs.update(extra)
    else:
        token = referral_append_url
        ref_keys = ["ref", "r", "start", "invite", "referral", "aff"]
        matched = False
        for k in ref_keys:
            if k in qs:
                qs[k] = [token]
                matched = True
                break
        if not matched:
            qs["ref"] = [token]

    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

# ─── Global state ─────────────────────────────────────────────────────────────

_MAX_EVENT_LOG   = 20    # entries kept in memory (newest first)
_EVENT_LOG_FILE  = "events.log"   # path relative to CWD (telegram-bot/)
_EVENT_LOG_DISK  = 500  # max lines kept on disk before rotation


class BotState:
    def __init__(self):
        self.running: bool = False
        self.paused: bool = False
        self.active_ip: str = "—"
        self.total_clicks: int = 0
        self.links_today: int = 0
        self.cycles_today: int = 0
        self.last_cycle_time: Optional[str] = None
        # Wizard conversation state keyed by user_id
        self.conv_step: dict[int, dict] = {}
        # Handle to the running automation task (for cancellation)
        self.loop_task: Optional[asyncio.Task] = None
        # Rolling in-memory event log (newest first)
        self.event_log: list[str] = []
        # Seed in-memory log from disk so history survives restarts
        self._load_event_log()

    # ── disk helpers ──────────────────────────────────────────────────────────

    def _load_event_log(self) -> None:
        """Read the last _MAX_EVENT_LOG lines from events.log (newest first)."""
        try:
            with open(_EVENT_LOG_FILE, "r", encoding="utf-8") as fh:
                lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
            # File is written oldest→newest; reverse for in-memory order
            self.event_log = lines[-_MAX_EVENT_LOG:][::-1]
        except FileNotFoundError:
            pass  # first run — no log yet, that's fine
        except Exception as exc:
            log.warning("Could not load %s: %s", _EVENT_LOG_FILE, exc)

    def _append_to_disk(self, entry: str) -> None:
        """Append one line to events.log and rotate if the file exceeds _EVENT_LOG_DISK lines."""
        try:
            # Append the new line
            with open(_EVENT_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(entry + "\n")

            # Rotate: keep only the newest _EVENT_LOG_DISK lines
            with open(_EVENT_LOG_FILE, "r", encoding="utf-8") as fh:
                all_lines = fh.readlines()

            if len(all_lines) > _EVENT_LOG_DISK:
                keep = all_lines[-_EVENT_LOG_DISK:]
                # Atomic write via temp file so a crash mid-write doesn't corrupt the log
                tmp = _EVENT_LOG_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.writelines(keep)
                import shutil as _shutil
                _shutil.move(tmp, _EVENT_LOG_FILE)
        except Exception as exc:
            log.warning("Could not write to %s: %s", _EVENT_LOG_FILE, exc)

    # ── public API ────────────────────────────────────────────────────────────

    def record_event(self, text: str) -> None:
        """Record a timestamped event in memory and persist it to disk."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = text.splitlines()[0] if text else ""
        entry = f"[{ts}] {summary}"
        # Memory (newest first, capped at _MAX_EVENT_LOG)
        self.event_log.insert(0, entry)
        if len(self.event_log) > _MAX_EVENT_LOG:
            self.event_log = self.event_log[:_MAX_EVENT_LOG]
        # Disk (oldest first, rotated at _EVENT_LOG_DISK)
        self._append_to_disk(entry)


state = BotState()

# ─── Playwright — human-like browser visit ───────────────────────────────────

async def playwright_visit(url: str, playwright_proxy: dict) -> bool:
    """
    Opens `url` in a headless Chromium browser via the session proxy.

    Anti-detection measures:
      - Realistic User-Agent and viewport
      - AutomationControlled feature disabled
      - Cookie banner auto-accept
      - Random smooth scroll to trigger lazy-load and ad impressions
      - Bounding-box mouse click on video player (never element.click())
      - Randomised watch time 65–110 s
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy=playwright_proxy,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.6367.155 Safari/537.36"
                ),
                viewport={"width": random.randint(1260, 1366), "height": random.randint(768, 900)},
                locale="en-US",
                timezone_id="America/New_York",
            )

            page = await ctx.new_page()

            # Auto-dismiss JS dialogs (alerts, confirms, prompts)
            page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

            log.info("Playwright → navigating to %s", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            except PWTimeout:
                log.warning("Playwright → page load timed out, continuing anyway.")
            except Exception as nav_err:
                log.warning("Playwright → navigation error: %s", nav_err)

            # ── Initial reading pause (4–9 s) ──────────────────────────────
            await asyncio.sleep(random.uniform(4.0, 9.0))

            # ── Accept cookie / consent banners ────────────────────────────
            cookie_selectors = [
                "button:has-text('Accept all')",
                "button:has-text('Accept')",
                "button:has-text('I agree')",
                "button:has-text('Agree')",
                "button:has-text('OK')",
                "button:has-text('Got it')",
                "[id*='cookie'] button",
                "[class*='cookie'] button",
                "[class*='consent'] button",
                "[aria-label*='Accept']",
            ]
            for sel in cookie_selectors:
                try:
                    btn = page.locator(sel).first
                    # scroll into view before checking visibility
                    await btn.scroll_into_view_if_needed(timeout=1_500)
                    await btn.click(timeout=2_000)
                    log.info("Playwright → dismissed cookie banner (%s)", sel)
                    await asyncio.sleep(random.uniform(0.7, 1.4))
                    break
                except Exception:
                    pass

            # ── Human-like scroll pattern ──────────────────────────────────
            # Scroll down in several steps (triggers lazy-load, ads)
            scroll_steps = random.randint(5, 10)
            for i in range(scroll_steps):
                direction = "down" if i < (scroll_steps * 0.7) else random.choice(["down", "up"])
                delta = random.randint(180, 520)
                await page.mouse.wheel(0, delta if direction == "down" else -delta)
                await asyncio.sleep(random.uniform(1.1, 3.4))

            # ── Video player — bounding-box click ─────────────────────────
            video_played = False
            video_selectors = [
                "video",
                ".video-player video",
                ".jwplayer video",
                "iframe[src*='youtube']",
                "iframe[src*='vimeo']",
                ".play-button",
                "[aria-label*='play' i]",
                "[data-testid*='play' i]",
            ]
            for v_sel in video_selectors:
                try:
                    elem = page.locator(v_sel).first
                    await elem.scroll_into_view_if_needed(timeout=2_000)
                    box = await elem.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        # Target the play button area — slightly above centre
                        cx = box["x"] + box["width"] / 2 + random.uniform(-15, 15)
                        cy = box["y"] + box["height"] / 2 + random.uniform(-10, 10)
                        # Natural mouse movement in multiple steps
                        await page.mouse.move(
                            cx + random.uniform(-80, 80),
                            cy + random.uniform(-50, 50),
                            steps=random.randint(8, 20),
                        )
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await page.mouse.move(cx, cy, steps=random.randint(5, 12))
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                        await page.mouse.click(cx, cy)
                        log.info("Playwright → clicked video element (%s) at (%.0f, %.0f)", v_sel, cx, cy)
                        video_played = True
                        break
                except Exception:
                    pass

            if not video_played:
                log.info("Playwright → no video element found; page will idle.")

            # ── Watch / idle time: 65–110 s ───────────────────────────────
            watch_time = random.uniform(65, 110)
            log.info("Playwright → holding page open for %.1f seconds.", watch_time)

            # During watch time, simulate occasional micro-scrolls to stay "active"
            elapsed = 0.0
            while elapsed < watch_time:
                chunk = min(random.uniform(8, 18), watch_time - elapsed)
                await asyncio.sleep(chunk)
                elapsed += chunk
                if elapsed < watch_time:
                    # Small micro-scroll to simulate viewer attention
                    await page.mouse.wheel(0, random.choice([-60, -40, 40, 60]))

            await browser.close()

        log.info("Playwright → session complete for %s", url)
        return True

    except Exception as exc:
        log.error("Playwright session failed for %s: %s", url, exc)
        return False

# ─── Userbot cycle (single pass over all tasks) ───────────────────────────────

async def run_userbot_cycle(
    cfg: dict,
    telethon_proxy: dict,
    playwright_proxy: dict,
    notify,
) -> None:
    """
    Connects the Telethon userbot via the pre-resolved proxy (pool entry or
    Asocks fallback), iterates every task in targets.json sequentially, clicks
    matching buttons, injects referral tokens, and launches the Playwright
    visit for each URL.  Fully disconnects on exit — no sockets remain open
    during the inter-cycle sleep, forcing a cold-boot next turn.
    """
    proxy = telethon_proxy

    client = TelegramClient(
        _get_userbot_session(),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            log.error(
                "Userbot session is not authorised. "
                "Run 'python main.py auth' locally and set SESSION_STRING on Railway."
            )
            await notify(
                "⚠️ Userbot session not authorised.\n\n"
                "Run this locally to generate a session string:\n"
                "`python main.py auth`\n\n"
                "Then set the printed value as the\n"
                "`SESSION_STRING` environment variable on Railway."
            )
            return

        # Reload targets fresh at the start of every cycle so any tasks added
        # via the control bot during the previous sleep are picked up.
        targets = load_targets()
        tasks = targets.get("tasks", [])

        if not tasks:
            log.info("No tasks in targets.json — skipping cycle.")
            await notify("ℹ️ No tasks configured. Add one with ➕.")
            return

        log.info("Cycle starting — %d task(s) to process.", len(tasks))

        for idx, task in enumerate(tasks, start=1):
            if not state.running or state.paused:
                log.info("Stop/pause signal received mid-cycle — aborting remaining tasks.")
                break

            raw_username = task.get("bot_username", "").strip().lstrip("@")
            wanted_buttons = {b.strip().lower() for b in task.get("buttons", []) if b.strip()}
            referral_append = task.get("referral_append_url", "").strip()

            if not raw_username:
                log.warning("Task %d has no bot_username — skipping.", idx)
                continue

            log.info("Task %d/%d — @%s", idx, len(tasks), raw_username)

            # ── Resolve entity ─────────────────────────────────────────────
            try:
                entity = await client.get_entity(raw_username)
            except Exception as exc:
                log.warning("Cannot resolve @%s: %s", raw_username, exc)
                await notify(f"⚠️ Cannot resolve @{raw_username}: {exc}")
                continue

            # ── Wake bot ───────────────────────────────────────────────────
            await client.send_message(entity, "/start")
            # Give the bot time to respond before fetching messages
            await asyncio.sleep(random.uniform(4.0, 7.0))

            # ── Fetch most recent messages (newest first) ──────────────────
            processed_any = False
            async for msg in client.iter_messages(entity, limit=15):
                if not state.running or state.paused:
                    break
                if not msg.reply_markup:
                    continue
                if not isinstance(msg.reply_markup, ReplyInlineMarkup):
                    continue

                for row in msg.reply_markup.rows:
                    for btn in row.buttons:
                        btn_text = (getattr(btn, "text", "") or "").strip()
                        if btn_text.lower() not in wanted_buttons:
                            continue

                        # ── Micro-delay before click (3.5–8.4 s) ──────────
                        delay = random.uniform(3.5, 8.4)
                        log.info(
                            "Waiting %.2fs before clicking '%s' on @%s",
                            delay, btn_text, raw_username,
                        )
                        await asyncio.sleep(delay)

                        extracted_url: Optional[str] = None

                        if isinstance(btn, KeyboardButtonUrl):
                            # URL button — URL is embedded in the button itself
                            extracted_url = btn.url
                            log.info("URL button → %s", extracted_url)

                        elif isinstance(btn, KeyboardButtonCallback):
                            # Callback button — must click and parse the bot's
                            # answer to get a URL (if any)
                            try:
                                answer = await msg.click(data=btn.data)
                                # BotCallbackAnswer may carry a URL
                                if answer and getattr(answer, "url", None):
                                    extracted_url = answer.url
                                    log.info("Callback answer URL → %s", extracted_url)
                                else:
                                    log.info(
                                        "Callback answer for '%s' has no URL "
                                        "(may have triggered an action on the bot).",
                                        btn_text,
                                    )
                            except Exception as cb_err:
                                log.warning("Callback click error on '%s': %s", btn_text, cb_err)

                        state.total_clicks += 1
                        processed_any = True

                        if extracted_url:
                            final_url = inject_referral(extracted_url, referral_append)
                            log.info("Injected referral → %s", final_url)

                            ok = await playwright_visit(final_url, playwright_proxy)

                            if ok:
                                state.links_today += 1
                                await notify(
                                    f"✅ Video watched successfully\n"
                                    f"Bot: @{raw_username}\n"
                                    f"Button: {btn_text}\n"
                                    f"URL: {final_url[:80]}…\n"
                                    f"IP: {state.active_ip}"
                                )
                            else:
                                await notify(
                                    f"⚠️ Playwright failed\n"
                                    f"Bot: @{raw_username} | Button: {btn_text}"
                                )

            if not processed_any:
                log.info("@%s — no matching buttons found in recent messages.", raw_username)

            log.info("Task @%s complete.", raw_username)

            # Small inter-task gap so we don't hammer Telegram's servers
            if idx < len(tasks):
                await asyncio.sleep(random.uniform(2.0, 5.0))

        state.cycles_today += 1
        state.last_cycle_time = datetime.now().strftime("%H:%M:%S")
        log.info("Cycle complete. Total cycles today: %d", state.cycles_today)

    except Exception as exc:
        log.error("Userbot cycle crashed: %s", exc, exc_info=True)
        await notify(f"❌ Cycle error: {exc}")
    finally:
        try:
            await client.disconnect()
            log.info("Userbot disconnected cleanly.")
        except Exception:
            pass

# ─── Automation loop ──────────────────────────────────────────────────────────

async def automation_loop(control_client: TelegramClient, cfg: dict) -> None:
    """
    Master loop: runs userbot cycles every 27–33 minutes until stopped.
    """
    owner_id = cfg["your_personal_telegram_id"]

    async def notify(text: str) -> None:
        state.record_event(text)          # always log, even if Telegram send fails
        try:
            await control_client.send_message(owner_id, text)
        except Exception as ne:
            log.warning("Notification send failed: %s", ne)

    try:
        while state.running and not state.paused:
            # One suffix per cycle → one brand-new Asocks IP for all work in
            # Resolve proxy for this cycle: pool entry (random) → Asocks fallback.
            # One suffix per cycle so all connections share the same Asocks session.
            suffix = fresh_session_suffix()
            t_proxy, r_proxy, pw_proxy, proxy_label = resolve_cycle_proxies(cfg, suffix)
            log.info("Proxy resolved: %s", proxy_label)

            # IP lookup — run in thread pool so the event loop isn't blocked
            state.active_ip = await get_current_ip_async(r_proxy)

            log.info("═══ Cycle start | IP: %s | Proxy: %s ═══", state.active_ip, proxy_label)
            await notify(
                f"🔄 Cycle #{state.cycles_today + 1} starting\n"
                f"🌐 IP: {state.active_ip}\n"
                f"🔀 Proxy: {proxy_label}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            await run_userbot_cycle(cfg, t_proxy, pw_proxy, notify)

            if not state.running or state.paused:
                break

            sleep_min = random.uniform(27, 33)
            sleep_sec = sleep_min * 60
            log.info("Sleeping %.1f minutes before next cycle.", sleep_min)
            await notify(
                f"💤 Cycle complete. Next cycle in {sleep_min:.1f} minutes.\n"
                f"Clicks this session: {state.total_clicks} | "
                f"Links today: {state.links_today}"
            )

            # Sleep in 5-second slices so stop/pause takes effect promptly
            elapsed = 0.0
            while elapsed < sleep_sec:
                if not state.running or state.paused:
                    break
                await asyncio.sleep(5)
                elapsed += 5

    except asyncio.CancelledError:
        log.info("Automation loop cancelled.")
    except Exception as exc:
        log.error("Automation loop crashed: %s", exc, exc_info=True)
        await notify(f"❌ Automation loop crashed: {exc}")
    finally:
        state.running = False
        state.loop_task = None
        await notify("🛑 Automation loop has stopped.")

# ─── Control Bot ──────────────────────────────────────────────────────────────

_KEYBOARD_ROWS = [
    ["🚀 تشغيل السكربت", "🛑 إيقاف مؤقت"],
    ["📊 تقرير الدورة الحالية", "🌐 فحص الـ IP الحالي"],
    ["➕ إضافة رابط/بوت جديد", "📋 عرض المهام"],
    ["🗑️ حذف مهمة", "📅 إعادة تعيين الإحصائيات"],
    ["📝 سجل الأحداث"],
]


def _build_kb() -> ReplyKeyboardMarkup:
    rows = [
        KeyboardButtonRow(buttons=[KeyboardButton(text=t) for t in row])
        for row in _KEYBOARD_ROWS
    ]
    return ReplyKeyboardMarkup(rows=rows, resize=True, persistent=True)


async def start_control_bot(cfg: dict) -> None:
    global _web_control_client, _web_cfg
    owner_id = cfg["your_personal_telegram_id"]

    control = TelegramClient(
        os.path.join(BASE_DIR, "control_bot_session"),
        cfg["api_id"],
        cfg["api_hash"],
    )
    await control.start(bot_token=cfg["control_bot_token"])

    # Expose client + cfg to the Flask /start endpoint
    _web_control_client = control
    _web_cfg = cfg

    kb = _build_kb()

    async def send(text: str) -> None:
        """Send a message to the owner with the persistent keyboard."""
        try:
            await control.send_message(owner_id, text, buttons=kb)
        except Exception as se:
            log.error("send() failed: %s", se)

    @control.on(events.NewMessage(from_users=owner_id))
    async def handler(event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip()

        # ── Multi-step wizard ─────────────────────────────────────────────
        conv = state.conv_step.get(owner_id)
        if conv:
            step = conv.get("step")

            if step == "ask_username":
                conv["bot_username"] = text
                conv["step"] = "ask_buttons"
                await event.reply(
                    "✏️ Step 2/3 — Send the button text(s) to click,\n"
                    "separated by commas (e.g. `Watch Video, Claim`):"
                )
                return

            if step == "ask_buttons":
                btns = [b.strip() for b in text.split(",") if b.strip()]
                if not btns:
                    await event.reply("⚠️ No buttons parsed. Please try again:")
                    return
                conv["buttons"] = btns
                conv["step"] = "ask_referral"
                await event.reply(
                    "🔗 Step 3/3 — Send the Referral Append URL or token\n"
                    "(type `None` to skip):"
                )
                return

            if step == "ask_referral":
                referral = "" if text.lower() in ("none", "skip", "-") else text
                new_task = {
                    "bot_username": (
                        conv["bot_username"]
                        if conv["bot_username"].startswith("@")
                        else "@" + conv["bot_username"]
                    ),
                    "buttons": conv["buttons"],
                    "referral_append_url": referral,
                }
                # Atomic load → append → atomic save
                data = load_targets()
                data["tasks"].append(new_task)
                save_targets(data)
                del state.conv_step[owner_id]

                await send(
                    f"✅ Task saved!\n"
                    f"Bot: {new_task['bot_username']}\n"
                    f"Buttons: {', '.join(new_task['buttons'])}\n"
                    f"Referral: {referral or '(none)'}\n\n"
                    f"Total tasks: {len(data['tasks'])}"
                )
                return

            if step == "ask_delete_number":
                data = load_targets()
                tasks = data.get("tasks", [])
                total = len(tasks)

                # Allow "cancel" to abort
                if text.lower() in ("cancel", "إلغاء", "0"):
                    del state.conv_step[owner_id]
                    await send("↩️ Delete cancelled. No changes made.")
                    return

                try:
                    idx = int(text)
                except ValueError:
                    await event.reply(
                        f"⚠️ Please send a number between 1 and {total}, "
                        f"or type `cancel` to abort:"
                    )
                    return

                if idx < 1 or idx > total:
                    await event.reply(
                        f"⚠️ Number out of range (1–{total}). Try again or type `cancel`:"
                    )
                    return

                removed = tasks.pop(idx - 1)
                data["tasks"] = tasks
                save_targets(data)
                del state.conv_step[owner_id]

                await send(
                    f"🗑️ Task #{idx} deleted!\n"
                    f"Removed: {removed.get('bot_username', '?')}\n\n"
                    f"Remaining tasks: {len(tasks)}"
                )
                return

        # ── Command dispatch ──────────────────────────────────────────────

        if text in ("/start", "/help"):
            await send(
                "🤖 Control Bot ready.\n"
                "Use the keyboard below to manage your automation."
            )

        elif text == "🚀 تشغيل السكربت":
            if state.running:
                await send("⚠️ Already running! Stop it first.")
                return
            state.running = True
            state.paused = False
            # Create the task and store the handle so we can cancel it later
            loop_coro = automation_loop(control, cfg)
            task = asyncio.ensure_future(loop_coro)
            state.loop_task = task

            # Log unhandled exceptions from the background task
            def _on_done(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception():
                    log.error("automation_loop unhandled exception: %s", t.exception())

            task.add_done_callback(_on_done)
            await send("✅ Automation loop started!")

        elif text == "🛑 إيقاف مؤقت":
            if not state.running and state.loop_task is None:
                await send("ℹ️ Not currently running.")
                return
            state.paused = True
            state.running = False
            # Cancel the task if it's sleeping between cycles
            if state.loop_task and not state.loop_task.done():
                state.loop_task.cancel()
            await send("⏸ Stop signal sent. Will halt after current operation completes.")

        elif text == "📊 تقرير الدورة الحالية":
            status = "🟢 Running" if state.running else "🔴 Stopped"
            targets = load_targets()
            task_count = len(targets.get("tasks", []))
            await send(
                f"📊 Status Report\n"
                f"{'─' * 20}\n"
                f"Status:          {status}\n"
                f"Active IP:       {state.active_ip}\n"
                f"Total clicks:    {state.total_clicks}\n"
                f"Links today:     {state.links_today}\n"
                f"Cycles today:    {state.cycles_today}\n"
                f"Last cycle:      {state.last_cycle_time or '—'}\n"
                f"Tasks loaded:    {task_count}"
            )

        elif text == "🌐 فحص الـ IP الحالي":
            await send("🔍 Checking IP via session proxy…")
            suffix = fresh_session_suffix()
            _, r_proxy, _, proxy_label = resolve_cycle_proxies(cfg, suffix)
            ip = await get_current_ip_async(r_proxy)
            state.active_ip = ip
            await send(f"🌐 Current IP: {ip}\n🔀 Proxy: {proxy_label}")

        elif text == "➕ إضافة رابط/بوت جديد":
            state.conv_step[owner_id] = {"step": "ask_username"}
            await event.reply(
                "➕ New Task Wizard — Step 1/3\n\n"
                "Send the bot username (e.g. @EarnBot):"
            )

        elif text == "📋 عرض المهام":
            targets = load_targets()
            tasks = targets.get("tasks", [])
            if not tasks:
                await send("📋 No tasks configured yet.\nUse ➕ to add your first bot.")
                return

            lines = [f"📋 Task List ({len(tasks)} total)\n{'─' * 22}"]
            for i, t in enumerate(tasks, start=1):
                username = t.get("bot_username", "(no username)")
                buttons = ", ".join(t.get("buttons", [])) or "(none)"
                referral = t.get("referral_append_url", "") or "(none)"
                lines.append(
                    f"\n#{i} — {username}\n"
                    f"  Buttons : {buttons}\n"
                    f"  Referral: {referral}"
                )
            lines.append(f"\n{'─' * 22}\nUse 🗑️ to delete a task by number.")
            await send("\n".join(lines))

        elif text == "🗑️ حذف مهمة":
            data = load_targets()
            tasks = data.get("tasks", [])
            if not tasks:
                await send("📋 No tasks to delete. Add one first with ➕.")
                return

            # Build a numbered preview so the user knows which number to send
            lines = [f"🗑️ Delete Task\n{'─' * 22}\nWhich task do you want to delete?\n"]
            for i, t in enumerate(tasks, start=1):
                lines.append(f"  #{i} — {t.get('bot_username', '?')}")
            lines.append(f"\n{'─' * 22}\nSend the task number (1–{len(tasks)})\nor type `cancel` to abort.")
            state.conv_step[owner_id] = {"step": "ask_delete_number"}
            await event.reply("\n".join(lines))

        elif text == "📅 إعادة تعيين الإحصائيات":
            old_clicks = state.total_clicks
            old_links = state.links_today
            old_cycles = state.cycles_today
            state.total_clicks = 0
            state.links_today = 0
            state.cycles_today = 0
            state.last_cycle_time = None
            await send(
                f"📅 Statistics reset!\n"
                f"{'─' * 22}\n"
                f"Cleared:\n"
                f"  Clicks : {old_clicks} → 0\n"
                f"  Links  : {old_links} → 0\n"
                f"  Cycles : {old_cycles} → 0\n\n"
                f"Reset at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

        elif text == "📝 سجل الأحداث":
            # Prefer in-memory log (fast); fall back to reading from disk
            # so history is available immediately after a restart.
            display = list(state.event_log)  # already newest-first
            source = "memory"

            if not display:
                try:
                    with open(_EVENT_LOG_FILE, "r", encoding="utf-8") as fh:
                        disk_lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
                    display = disk_lines[-_MAX_EVENT_LOG:][::-1]  # newest first
                    source = "disk"
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    log.warning("Event log read error: %s", exc)

            if not display:
                await send(
                    "📝 Event Log\n"
                    "──────────────────────\n"
                    "No events recorded yet.\n"
                    "Start the automation with 🚀 to begin logging."
                )
                return

            header = (
                f"📝 Event Log — {len(display)} events"
                + (" (from disk 💾)" if source == "disk" else "")
                + f"\n{'─' * 22}"
            )
            footer = (
                f"{'─' * 22}\n"
                f"Showing newest → oldest\n"
                f"Full log: {_EVENT_LOG_FILE} ({_EVENT_LOG_DISK}-line rolling file)"
            )
            await send("\n".join([header] + display + [footer]))

        else:
            # Unknown text while not in wizard — silently ignore
            pass

    log.info("Control Bot connected. Greeting owner…")
    try:
        await control.send_message(
            owner_id,
            "🤖 Control Bot is online and ready!\nUse the keyboard to manage automation.",
            buttons=kb,
        )
    except Exception as ge:
        log.warning("Could not send startup greeting: %s", ge)

    await control.run_until_disconnected()

# ─── Flask health server ──────────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    status = "running" if state.running else "stopped"
    return (
        f"OK | status={status} | "
        f"clicks={state.total_clicks} | "
        f"links_today={state.links_today} | "
        f"cycles={state.cycles_today} | "
        f"ip={state.active_ip}",
        200,
    )


@flask_app.route("/status")
def status_webhook():
    import json as _json
    from datetime import datetime as _dt

    payload = {
        "status": "running" if state.running else ("paused" if state.paused else "stopped"),
        "running": state.running,
        "paused": state.paused,
        "proxy_ip": state.active_ip,
        "total_clicks": state.total_clicks,
        "links_today": state.links_today,
        "cycles_today": state.cycles_today,
        "last_cycle": state.last_cycle_time,
        "timestamp": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recent_events": state.event_log[:5],
    }
    return flask_app.response_class(
        response=_json.dumps(payload, ensure_ascii=False, indent=2),
        status=200,
        mimetype="application/json",
    )


# Global reference to the main asyncio event loop, set at startup so the
# Flask thread can safely schedule coroutines onto it.
_main_loop: asyncio.AbstractEventLoop | None = None


def _check_webhook_token() -> "flask.Response | None":
    """
    Validates the WEBHOOK_SECRET token on protected endpoints.

    Accepted formats (either is fine):
      • Header:  Authorization: Bearer <token>
      • Query:   ?token=<token>

    Returns None when the token is valid (caller proceeds).
    Returns a 401/503 JSON Response when access should be denied.

    If WEBHOOK_SECRET is not set the endpoint is open — log a warning so
    the operator knows to set the variable.
    """
    import json as _json
    from flask import request as _req

    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        log.warning(
            "WEBHOOK_SECRET is not set — /start and /stop are unprotected. "
            "Set this env var on Railway to secure your endpoints."
        )
        return None

    # Accept token from Authorization header or ?token= query param
    auth_header = _req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[len("Bearer "):]
    else:
        provided = _req.args.get("token", "")

    if not provided or provided != secret:
        return flask_app.response_class(
            response=_json.dumps({"ok": False, "message": "Unauthorized."}),
            status=401,
            mimetype="application/json",
        )
    return None


@flask_app.route("/start", methods=["POST"])
def start_endpoint():
    import json as _json

    denied = _check_webhook_token()
    if denied:
        return denied

    if state.running:
        return flask_app.response_class(
            response=_json.dumps({"ok": False, "message": "Already running."}),
            status=409,
            mimetype="application/json",
        )

    if _main_loop is None or not _main_loop.is_running():
        return flask_app.response_class(
            response=_json.dumps({"ok": False, "message": "Event loop not ready."}),
            status=503,
            mimetype="application/json",
        )

    def _launch():
        state.running = True
        state.paused = False
        task = asyncio.ensure_future(
            automation_loop(_web_control_client, _web_cfg), loop=_main_loop
        )
        state.loop_task = task

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                log.error("automation_loop (web-start) error: %s", t.exception())

        task.add_done_callback(_on_done)

    _main_loop.call_soon_threadsafe(_launch)
    return flask_app.response_class(
        response=_json.dumps({"ok": True, "message": "Automation started."}),
        status=200,
        mimetype="application/json",
    )


@flask_app.route("/stop", methods=["POST"])
def stop_endpoint():
    import json as _json

    denied = _check_webhook_token()
    if denied:
        return denied

    if not state.running and state.loop_task is None:
        return flask_app.response_class(
            response=_json.dumps({"ok": False, "message": "Not currently running."}),
            status=409,
            mimetype="application/json",
        )

    def _halt():
        state.paused = True
        state.running = False
        if state.loop_task and not state.loop_task.done():
            state.loop_task.cancel()

    if _main_loop and _main_loop.is_running():
        _main_loop.call_soon_threadsafe(_halt)
    else:
        _halt()

    return flask_app.response_class(
        response=_json.dumps({"ok": True, "message": "Stop signal sent."}),
        status=200,
        mimetype="application/json",
    )


# Populated by start_control_bot() once the control client and config are live.
_web_control_client: TelegramClient | None = None
_web_cfg: dict | None = None


def start_flask() -> None:
    port = int(os.environ.get("PORT", 8080))
    log.info("Flask health server listening on 0.0.0.0:%d", port)
    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )

# ─── First-time userbot auth ──────────────────────────────────────────────────

async def first_time_auth(cfg: dict) -> None:
    """
    Interactive one-time flow to authorise the userbot and generate a session.

    Telethon will prompt for phone number → OTP → (optional) 2FA password.
    Two artefacts are produced:
      1. userbot_session.session  — local file (works for local dev)
      2. SESSION_STRING           — printed to stdout; set this as a Railway
                                    environment variable so the session survives
                                    across deployments on ephemeral filesystems.

    This command only needs to be run once (or whenever the session is revoked).
    """
    proxy = build_telethon_proxy(cfg, fresh_session_suffix())

    # Always use a file-based session for auth so the interactive prompt works
    # reliably and the .session file is available for local dev too.
    session_path = os.path.join(BASE_DIR, "userbot_session")
    client = TelegramClient(
        session_path,
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )
    await client.start()
    me = await client.get_me()

    # Export as a portable string (needed for Railway / any cloud platform)
    session_string = client.session.save()

    await client.disconnect()

    print(f"\n✅ Authorised as: {me.first_name} (@{me.username})")
    print(f"   Local session file saved to: {session_path}.session\n")
    print("━" * 64)
    print("  SESSION STRING — copy this for Railway / cloud deployment")
    print("━" * 64)
    print()
    print(session_string)
    print()
    print("━" * 64)
    print("  NEXT STEPS:")
    print("  1. Copy the string printed above (the long base64-ish value).")
    print("  2. In your Railway project → Variables, add:")
    print("       SESSION_STRING = <paste here>")
    print("  3. Redeploy / restart the Railway service.")
    print("  4. The Control Bot will message you on Telegram once it's live.")
    print("━" * 64)
    print()

# ─── Saved Messages proxy listener ───────────────────────────────────────────

_PROXY_LINE_RE = re.compile(
    r"""
    (?:(?P<proto>https?|socks[45])://)?   # optional protocol prefix
    (?P<host>
        (?:\d{1,3}\.){3}\d{1,3}          # IPv4
        |localhost
        |(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}  # hostname
    )
    :(?P<port>\d{2,5})                    # :PORT
    """,
    re.VERBOSE,
)


def _parse_proxy_lines(text: str) -> list[str]:
    """
    Extract and normalise proxy entries from a block of text.

    Rules:
    • Each line is evaluated independently.
    • Lines already containing a recognised protocol are kept verbatim
      (stripped).
    • Lines that match bare  HOST:PORT  are prefixed with  http://
    • Any line that does not match the pattern is silently skipped.
    • Port must be 1–65535; entries outside that range are dropped.
    """
    results = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROXY_LINE_RE.search(line)
        if not m:
            continue
        port = int(m.group("port"))
        if not (1 <= port <= 65535):
            continue
        proto = (m.group("proto") or "http").lower()
        host  = m.group("host")
        results.append(f"{proto}://{host}:{port}")
    return results


def _append_unique_proxies(new_proxies: list[str]) -> int:
    """
    Thread-safe append of new_proxies to proxies.txt.
    Reads existing entries, merges, deduplicates (order-preserving),
    writes back atomically.  Returns the count of genuinely new entries added.
    """
    with _proxies_lock:
        # Read existing
        existing: list[str] = []
        try:
            with open(PROXIES_PATH, "r", encoding="utf-8") as fh:
                existing = [ln.strip() for ln in fh if ln.strip()]
        except FileNotFoundError:
            pass

        existing_set = set(existing)
        added = [p for p in new_proxies if p not in existing_set]
        if not added:
            return 0

        merged = existing + added
        # Atomic write
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=BASE_DIR, prefix=".proxies_tmp_", suffix=".txt"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(merged) + "\n")
            shutil.move(tmp_path, PROXIES_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return len(added)


async def start_proxy_listener(cfg: dict) -> None:
    """
    Persistent userbot client that watches the owner's Saved Messages for
    proxy blocks.

    When you send yourself a message containing one or more  IP:PORT  lines
    (or fully-qualified  protocol://host:port  entries), this listener:
      1. Parses every valid proxy line in the message.
      2. Appends only new (non-duplicate) entries to  proxies.txt.
      3. Replies to Saved Messages with a live confirmation.

    The client runs independently of the 30-minute click loop — it never
    disconnects unless the process exits.
    """
    listener = TelegramClient(
        _get_userbot_session(),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=build_telethon_proxy(cfg, fresh_session_suffix()),
    )
    await listener.connect()

    if not await listener.is_user_authorized():
        log.error(
            "Proxy listener: userbot not authorised — set SESSION_STRING. "
            "Listener will not start."
        )
        await listener.disconnect()
        return

    @listener.on(events.NewMessage(from_users="me", incoming=False, outgoing=True))
    async def _on_saved_message(event):
        """Fires whenever the owner sends a message to their Saved Messages."""
        text = event.raw_text or ""
        parsed = _parse_proxy_lines(text)
        if not parsed:
            return  # Not a proxy block — ignore silently

        try:
            added = _append_unique_proxies(parsed)
        except Exception as exc:
            log.error("proxies.txt write error: %s", exc)
            await event.reply(f"❌ Error saving proxies: {exc}")
            return

        total = len(parsed)
        duplicates = total - added
        msg = (
            f"✅ Successfully parsed and saved {added} new unique "
            f"{'proxy' if added == 1 else 'proxies'} to the file!\n"
            f"📋 Total parsed: {total} | Duplicates skipped: {duplicates}"
        )
        log.info("Proxy listener: %s", msg)
        await event.reply(msg)

    log.info("Proxy listener active — watching Saved Messages for proxy blocks.")
    await listener.run_until_disconnected()


# ─── Daily stats reset ────────────────────────────────────────────────────────

async def midnight_reset_loop() -> None:
    """
    Runs forever in the background.  At every UTC midnight it resets the
    per-day counters (links_today, cycles_today) and sends a summary message
    to the owner via the control bot before clearing the numbers.

    Sleep is calculated precisely so the reset fires within one second of
    00:00:00 UTC regardless of when the process started.
    """
    while True:
        now = datetime.utcnow()
        # Seconds until the next UTC midnight
        seconds_until_midnight = (
            (23 - now.hour) * 3600
            + (59 - now.minute) * 60
            + (60 - now.second)
        )
        log.info(
            "Daily reset scheduled in %dh %dm %ds (UTC midnight).",
            seconds_until_midnight // 3600,
            (seconds_until_midnight % 3600) // 60,
            seconds_until_midnight % 60,
        )
        await asyncio.sleep(seconds_until_midnight)

        # Snapshot before clearing so the summary is accurate
        links  = state.links_today
        cycles = state.cycles_today

        state.links_today   = 0
        state.cycles_today  = 0
        state.record_event(
            f"🔄 Daily reset — links: {links}, cycles: {cycles} → counters cleared."
        )
        log.info("Daily reset complete. links_today and cycles_today reset to 0.")

        # Notify the owner if the control bot is already connected
        if _web_control_client and _web_cfg:
            try:
                await _web_control_client.send_message(
                    _web_cfg["your_personal_telegram_id"],
                    f"🌅 Daily Reset (UTC midnight)\n"
                    f"{'─' * 22}\n"
                    f"Yesterday's links:  {links}\n"
                    f"Yesterday's cycles: {cycles}\n"
                    f"Counters reset to 0 — new day started!",
                )
            except Exception as exc:
                log.warning("Could not send daily-reset notification: %s", exc)

        # Brief pause so we don't fire twice if we wake up a fraction early
        await asyncio.sleep(2)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    global _main_loop
    cfg = load_config()

    # Capture the running event loop so Flask threads can schedule coroutines.
    _main_loop = asyncio.get_event_loop()

    # Flask runs on a daemon thread — Railway sees the port and won't kill the app
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Launch the daily stats reset in the background (fires every UTC midnight)
    asyncio.ensure_future(midnight_reset_loop())

    # Launch the Saved Messages proxy listener in the background (non-blocking)
    asyncio.ensure_future(start_proxy_listener(cfg))

    # Block on the control bot (runs until process is killed or bot disconnects)
    await start_control_bot(cfg)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        cfg = load_config()
        asyncio.run(first_time_auth(cfg))
    else:
        asyncio.run(main())
