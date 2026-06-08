import asyncio
import json
import logging
import os
import random
import re
import threading
import time
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
)

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── Config helpers ───────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TARGETS_PATH = os.path.join(BASE_DIR, "targets.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_targets() -> dict:
    with open(TARGETS_PATH, "r") as f:
        return json.load(f)


def save_targets(data: dict) -> None:
    with open(TARGETS_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── Proxy helpers ────────────────────────────────────────────────────────────

def fresh_session_token() -> str:
    return f"-session_{random.randint(100000, 999999)}"


def build_proxy_dict(cfg: dict, session_suffix: str) -> dict:
    username = cfg["proxy_username"] + session_suffix
    return {
        "proxy_type": "socks5",
        "addr": cfg["proxy_host"],
        "port": cfg["proxy_port"],
        "username": username,
        "password": cfg["proxy_password"],
        "rdns": True,
    }


def build_requests_proxy(cfg: dict, session_suffix: str) -> dict:
    username = cfg["proxy_username"] + session_suffix
    url = (
        f"socks5://{username}:{cfg['proxy_password']}"
        f"@{cfg['proxy_host']}:{cfg['proxy_port']}"
    )
    return {"http": url, "https": url}


def get_current_ip(cfg: dict, session_suffix: str) -> str:
    try:
        proxies = build_requests_proxy(cfg, session_suffix)
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
        return r.json().get("ip", "unknown")
    except Exception as exc:
        log.warning("IP check failed: %s", exc)
        return "unavailable"


# ─── Referral URL injection ───────────────────────────────────────────────────

def inject_referral(url: str, referral_append_url: str) -> str:
    """
    Appends or substitutes a referral parameter inside `url`.

    Strategy:
      1. If `referral_append_url` is empty/None, return url unchanged.
      2. If the referral value looks like a full URL fragment ("?ref=xxx"), merge
         its query params into the target URL.
      3. Otherwise treat `referral_append_url` as a raw token and try to replace
         an existing `ref`, `r`, `start`, or `invite` query param; if none found,
         append ?ref=<token>.
    """
    if not referral_append_url:
        return url

    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    if referral_append_url.startswith("?") or "=" in referral_append_url:
        extra = parse_qs(referral_append_url.lstrip("?"), keep_blank_values=True)
        qs.update(extra)
    else:
        token = referral_append_url
        ref_keys = ["ref", "r", "start", "invite", "referral"]
        matched = False
        for k in ref_keys:
            if k in qs:
                qs[k] = [token]
                matched = True
                break
        if not matched:
            qs["ref"] = [token]

    new_query = urlencode(qs, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)


# ─── State ────────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.running = False
        self.paused = False
        self.active_ip: str = "—"
        self.total_clicks: int = 0
        self.links_today: int = 0
        self.cycles_today: int = 0
        self.last_cycle_time: Optional[str] = None

        # Conversation state for "add new task" flow
        self.conv_step: dict[int, dict] = {}


state = BotState()

# ─── Playwright browser simulation ───────────────────────────────────────────

async def playwright_visit(url: str, cfg: dict, session_suffix: str) -> bool:
    """
    Launches a headless Playwright browser through the session proxy,
    opens `url`, simulates human behaviour, and returns True on success.
    """
    try:
        from playwright.async_api import async_playwright

        proxy_cfg = {
            "server": f"socks5://{cfg['proxy_host']}:{cfg['proxy_port']}",
            "username": cfg["proxy_username"] + session_suffix,
            "password": cfg["proxy_password"],
        }

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()

            # Auto-dismiss dialogs
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

            log.info("Playwright → opening %s", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as nav_err:
                log.warning("Navigation error (continuing): %s", nav_err)

            # Accept cookie banners
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('I agree')",
                "button:has-text('OK')",
                "[id*='cookie'] button",
                "[class*='consent'] button",
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception:
                    pass

            # Simulate reading — random scroll up/down
            for _ in range(random.randint(4, 8)):
                direction = random.choice(["down", "up"])
                pixels = random.randint(200, 600)
                await page.mouse.wheel(0, pixels if direction == "down" else -pixels)
                await asyncio.sleep(random.uniform(1.2, 3.1))

            # Try to find & play a video element naturally
            try:
                video = page.locator("video").first
                if await video.is_visible(timeout=3_000):
                    box = await video.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2 + random.uniform(-10, 10)
                        cy = box["y"] + box["height"] / 2 + random.uniform(-10, 10)
                        await page.mouse.move(cx, cy, steps=random.randint(10, 25))
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                        await page.mouse.click(cx, cy)
                        log.info("Playwright → clicked video Play")
            except Exception:
                pass

            # Watch time: 65 – 110 seconds
            watch_time = random.uniform(65, 110)
            log.info("Playwright → watching for %.1f seconds", watch_time)
            await asyncio.sleep(watch_time)

            await browser.close()
        return True

    except Exception as exc:
        log.error("Playwright session failed: %s", exc)
        return False


# ─── Userbot cycle ────────────────────────────────────────────────────────────

async def run_userbot_cycle(
    cfg: dict,
    session_suffix: str,
    notify_fn,
) -> None:
    """
    One full automation cycle:
      - Connect the Telethon userbot via fresh proxy
      - For every task in targets.json, send /start, iterate button rows,
        click matching buttons, extract URLs, inject referral, open in Playwright
      - Disconnect and update state
    """
    proxy = build_proxy_dict(cfg, session_suffix)
    client = TelegramClient(
        os.path.join(BASE_DIR, "userbot_session"),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Userbot session not authorised — run first-time auth separately.")
            await notify_fn("⚠️ Userbot session not authorised. Run first-time auth.")
            await client.disconnect()
            return

        targets = load_targets()

        for task in targets.get("tasks", []):
            bot_username = task.get("bot_username", "").lstrip("@")
            wanted_buttons = [b.lower() for b in task.get("buttons", [])]
            referral_append = task.get("referral_append_url", "")

            log.info("Processing task: @%s", bot_username)

            try:
                entity = await client.get_entity(bot_username)
            except Exception as exc:
                log.warning("Could not resolve @%s: %s", bot_username, exc)
                await notify_fn(f"⚠️ Cannot resolve @{bot_username}: {exc}")
                continue

            # Send /start to wake the bot
            await client.send_message(entity, "/start")
            await asyncio.sleep(random.uniform(3.5, 6.0))

            # Fetch recent messages to find inline keyboard
            found_url: Optional[str] = None
            async for msg in client.iter_messages(entity, limit=10):
                if not msg.reply_markup:
                    continue
                if not isinstance(msg.reply_markup, ReplyInlineMarkup):
                    continue
                for row in msg.reply_markup.rows:
                    for btn in row.buttons:
                        btn_text = getattr(btn, "text", "") or ""
                        if btn_text.lower() not in wanted_buttons:
                            continue

                        # Micro-delay before click
                        delay = random.uniform(3.5, 8.4)
                        log.info("Waiting %.2fs before clicking '%s'", delay, btn_text)
                        await asyncio.sleep(delay)

                        if isinstance(btn, KeyboardButtonUrl):
                            found_url = btn.url
                            log.info("Button URL extracted: %s", found_url)
                        elif isinstance(btn, KeyboardButtonCallback):
                            try:
                                result = await client.request_url(
                                    entity, msg.id, btn.data
                                )
                            except Exception:
                                pass
                            try:
                                clicked = await msg.click(data=btn.data)
                                if hasattr(clicked, "url"):
                                    found_url = clicked.url
                            except Exception as ce:
                                log.warning("Callback click error: %s", ce)

                        state.total_clicks += 1

                        if found_url:
                            final_url = inject_referral(found_url, referral_append)
                            log.info("Final URL → %s", final_url)
                            ok = await playwright_visit(final_url, cfg, session_suffix)
                            if ok:
                                state.links_today += 1
                                await notify_fn(
                                    f"✅ Successfully watched video\n"
                                    f"Bot: @{bot_username}\n"
                                    f"Button: {btn_text}\n"
                                    f"IP: {state.active_ip}"
                                )
                            else:
                                await notify_fn(
                                    f"⚠️ Playwright failed for @{bot_username} / {btn_text}"
                                )
                            found_url = None

            log.info("Task @%s done.", bot_username)

        state.cycles_today += 1
        state.last_cycle_time = datetime.now().strftime("%H:%M:%S")

    except Exception as exc:
        log.error("Userbot cycle error: %s", exc)
        await notify_fn(f"❌ Cycle error: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ─── Main automation loop ─────────────────────────────────────────────────────

async def automation_loop(control_client: TelegramClient, cfg: dict) -> None:
    """
    Runs the userbot in 27-33 minute cycles until state.paused is set.
    """
    async def notify(text: str):
        try:
            await control_client.send_message(
                cfg["your_personal_telegram_id"], text
            )
        except Exception as ne:
            log.warning("Notify failed: %s", ne)

    while state.running and not state.paused:
        session_suffix = fresh_session_token()
        state.active_ip = get_current_ip(cfg, session_suffix)

        log.info("=== Cycle start | IP: %s ===", state.active_ip)
        await notify(
            f"🔄 New cycle started\n"
            f"🌐 IP: {state.active_ip}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )

        await run_userbot_cycle(cfg, session_suffix, notify)

        if not state.running or state.paused:
            break

        sleep_min = random.uniform(27, 33)
        sleep_sec = sleep_min * 60
        log.info("Sleeping %.1f minutes until next cycle.", sleep_min)
        await notify(f"💤 Sleeping {sleep_min:.1f} minutes until next cycle…")

        # Sleep in small chunks so we can honour a pause/stop quickly
        chunks = int(sleep_sec / 5)
        for _ in range(chunks):
            if not state.running or state.paused:
                break
            await asyncio.sleep(5)

    state.running = False
    await notify("🛑 Automation loop has stopped.")


# ─── Control Bot ──────────────────────────────────────────────────────────────

KEYBOARD = [
    ["🚀 تشغيل السكربت", "🛑 إيقاف مؤقت"],
    ["📊 تقرير الدورة الحالية", "🌐 فحص الـ IP الحالي"],
    ["➕ إضافة رابط/بوت جديد"],
]

KEYBOARD_MARKUP = {
    "keyboard": KEYBOARD,
    "resize_keyboard": True,
    "persistent": True,
}


def build_telethon_keyboard():
    from telethon.tl.types import (
        ReplyKeyboardMarkup,
        KeyboardButtonRow,
        KeyboardButton,
    )
    rows = []
    for row in KEYBOARD:
        rows.append(KeyboardButtonRow(buttons=[KeyboardButton(text=t) for t in row]))
    return ReplyKeyboardMarkup(rows=rows, resize=True, persistent=True)


async def start_control_bot(cfg: dict) -> None:
    owner_id = cfg["your_personal_telegram_id"]

    control = TelegramClient(
        os.path.join(BASE_DIR, "control_bot_session"),
        cfg["api_id"],
        cfg["api_hash"],
    )
    await control.start(bot_token=cfg["control_bot_token"])

    kb = build_telethon_keyboard()

    async def send_kb(text: str):
        await control.send_message(owner_id, text, buttons=kb)

    loop_task: Optional[asyncio.Task] = None

    @control.on(events.NewMessage(from_users=owner_id))
    async def handler(event):
        nonlocal loop_task
        text = event.raw_text.strip()

        # ── Conversation flow ──────────────────────────────────────────────
        conv = state.conv_step.get(owner_id)
        if conv:
            step = conv.get("step")

            if step == "ask_username":
                conv["bot_username"] = text
                conv["step"] = "ask_buttons"
                await event.reply(
                    "✏️ Please send the button text(s) separated by commas:"
                )
                return

            if step == "ask_buttons":
                conv["buttons"] = [b.strip() for b in text.split(",") if b.strip()]
                conv["step"] = "ask_referral"
                await event.reply(
                    "🔗 Please send the Referral Append URL (or type None):"
                )
                return

            if step == "ask_referral":
                referral = "" if text.lower() == "none" else text
                new_task = {
                    "bot_username": conv["bot_username"],
                    "buttons": conv["buttons"],
                    "referral_append_url": referral,
                }
                data = load_targets()
                data["tasks"].append(new_task)
                save_targets(data)
                del state.conv_step[owner_id]
                await send_kb(
                    f"✅ Task added and saved!\n"
                    f"Bot: {new_task['bot_username']}\n"
                    f"Buttons: {', '.join(new_task['buttons'])}\n"
                    f"Referral: {referral or '(none)'}"
                )
                return

        # ── Command dispatch ───────────────────────────────────────────────

        if text in ("/start", "/help"):
            await send_kb(
                "👋 Control Bot is online.\nUse the keyboard below to manage the automation."
            )

        elif text == "🚀 تشغيل السكربت":
            if state.running:
                await send_kb("⚠️ Already running!")
                return
            state.running = True
            state.paused = False
            loop_task = asyncio.ensure_future(automation_loop(control, cfg))
            await send_kb("✅ Automation loop started!")

        elif text == "🛑 إيقاف مؤقت":
            if not state.running:
                await send_kb("ℹ️ Not running.")
                return
            state.paused = True
            state.running = False
            await send_kb("⏸ Pausing after current cycle completes…")

        elif text == "📊 تقرير الدورة الحالية":
            status = "🟢 Running" if state.running else "🔴 Stopped"
            await send_kb(
                f"📊 Status Report\n"
                f"──────────────\n"
                f"Status: {status}\n"
                f"Active IP: {state.active_ip}\n"
                f"Total clicks: {state.total_clicks}\n"
                f"Links processed today: {state.links_today}\n"
                f"Cycles today: {state.cycles_today}\n"
                f"Last cycle: {state.last_cycle_time or '—'}"
            )

        elif text == "🌐 فحص الـ IP الحالي":
            await send_kb("🔍 Checking IP through proxy…")
            suffix = fresh_session_token()
            ip = get_current_ip(cfg, suffix)
            await send_kb(f"🌐 Current US IP: {ip}")

        elif text == "➕ إضافة رابط/بوت جديد":
            state.conv_step[owner_id] = {"step": "ask_username"}
            await event.reply(
                "➕ New Task Wizard\nStep 1/3 — Please send the Bot Username (e.g. @YourBot):"
            )

    log.info("Control Bot started. Sending greeting to owner…")
    try:
        await control.send_message(owner_id, "🤖 Control Bot is online!", buttons=kb)
    except Exception as ge:
        log.warning("Could not send greeting: %s", ge)

    await control.run_until_disconnected()


# ─── Flask health server ──────────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return (
        f"OK — Automation running: {state.running} | "
        f"Clicks: {state.total_clicks} | "
        f"Links today: {state.links_today}",
        200,
    )


def start_flask() -> None:
    port = int(os.environ.get("PORT", 8080))
    log.info("Flask health server on port %d", port)
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ─── First-time userbot auth (run standalone) ─────────────────────────────────

async def first_time_auth(cfg: dict) -> None:
    """
    Run this once manually to authorise the userbot session.
    Usage: python main.py auth
    """
    proxy = build_proxy_dict(cfg, fresh_session_token())
    client = TelegramClient(
        os.path.join(BASE_DIR, "userbot_session"),
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )
    await client.start()
    print("✅ Userbot session authorised and saved.")
    await client.disconnect()


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    cfg = load_config()

    # Start Flask on a background thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Run the control bot (blocks until disconnected)
    await start_control_bot(cfg)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        cfg = load_config()
        asyncio.run(first_time_auth(cfg))
    else:
        asyncio.run(main())
