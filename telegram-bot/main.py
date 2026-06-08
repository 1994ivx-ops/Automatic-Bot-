"""
Dual-bot Telegram + Browser automation system.
  - Userbot:      Telethon user session, iterates all tasks, clicks buttons,
                  injects referral tokens, opens links in Playwright.
  - Control Bot:  Telegram bot (token-based) for the owner to start/stop/monitor.
  - Flask:        Lightweight health server for Railway port-binding.

Run once for first-time auth:
    python main.py auth

Normal run:
    python main.py
"""

import asyncio
import json
import logging
import os
import random
import threading
import tempfile
import shutil
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from flask import Flask
from telethon import TelegramClient, events
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
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TARGETS_PATH = os.path.join(BASE_DIR, "targets.json")

# File-level lock — guards all reads AND writes to targets.json so that a
# concurrent control-bot write never races with the userbot's load.
_targets_lock = threading.Lock()

# ─── Config / Targets helpers ─────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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

def fresh_session_suffix() -> str:
    """
    Returns a unique suffix injected into the proxy username per-cycle.
    Every call produces a different random integer, guaranteeing IP rotation.
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


async def get_current_ip_async(cfg: dict, suffix: str) -> str:
    """
    Non-blocking IP lookup through the session proxy.
    Runs the blocking `requests.get` in a thread pool so it never stalls
    the asyncio event loop.
    """
    def _fetch():
        try:
            proxies = build_requests_proxy(cfg, suffix)
            r = requests.get(
                "https://api.ipify.org?format=json", proxies=proxies, timeout=15
            )
            return r.json().get("ip", "unknown")
        except Exception as exc:
            log.warning("IP check failed: %s", exc)
            return "unavailable"

    return await asyncio.to_thread(_fetch)

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

async def run_userbot_cycle(cfg: dict, suffix: str, notify) -> None:
    """
    Connects the Telethon userbot via a fresh proxy session, iterates every
    task in targets.json sequentially, clicks matching buttons, injects
    referral tokens, and launches the Playwright visit for each extracted URL.
    Always disconnects on exit, even if an exception occurs.
    """
    proxy = build_telethon_proxy(cfg, suffix)
    playwright_proxy = build_playwright_proxy(cfg, suffix)

    client = TelegramClient(
        os.path.join(BASE_DIR, "userbot_session"),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            log.error("Userbot session is not authorised. Run: python main.py auth")
            await notify(
                "⚠️ Userbot session not authorised.\n"
                "SSH into the server and run:\n`python main.py auth`"
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
        try:
            await control_client.send_message(owner_id, text)
        except Exception as ne:
            log.warning("Notification send failed: %s", ne)

    try:
        while state.running and not state.paused:
            # Fresh session token = fresh IP for every cycle
            suffix = fresh_session_suffix()

            # IP lookup is blocking — run in thread pool
            state.active_ip = await get_current_ip_async(cfg, suffix)

            log.info("═══ Cycle start | IP: %s ═══", state.active_ip)
            await notify(
                f"🔄 Cycle #{state.cycles_today + 1} starting\n"
                f"🌐 IP: {state.active_ip}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            await run_userbot_cycle(cfg, suffix, notify)

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
]


def _build_kb() -> ReplyKeyboardMarkup:
    rows = [
        KeyboardButtonRow(buttons=[KeyboardButton(text=t) for t in row])
        for row in _KEYBOARD_ROWS
    ]
    return ReplyKeyboardMarkup(rows=rows, resize=True, persistent=True)


async def start_control_bot(cfg: dict) -> None:
    owner_id = cfg["your_personal_telegram_id"]

    control = TelegramClient(
        os.path.join(BASE_DIR, "control_bot_session"),
        cfg["api_id"],
        cfg["api_hash"],
    )
    await control.start(bot_token=cfg["control_bot_token"])

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
            ip = await get_current_ip_async(cfg, suffix)
            state.active_ip = ip
            await send(f"🌐 Current IP: {ip}\nSession: …{suffix[-10:]}")

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
    Interactive one-time flow to create and save the userbot session file.
    Telethon will prompt for phone number → OTP → (optional) 2FA password.
    The session is saved to userbot_session.session and never needs to be
    repeated unless the session is revoked.
    """
    proxy = build_telethon_proxy(cfg, fresh_session_suffix())
    client = TelegramClient(
        os.path.join(BASE_DIR, "userbot_session"),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Authorised as: {me.first_name} (@{me.username}) — session saved.\n")
    await client.disconnect()

# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    cfg = load_config()

    # Flask runs on a daemon thread — Railway sees the port and won't kill the app
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Block on the control bot (runs until process is killed or bot disconnects)
    await start_control_bot(cfg)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        cfg = load_config()
        asyncio.run(first_time_auth(cfg))
    else:
        asyncio.run(main())
