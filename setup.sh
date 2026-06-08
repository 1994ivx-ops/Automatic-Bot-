#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  —  One-shot setup for the Telegram + Browser automation project
# Run this once on the server before starting main.py for the first time.
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo ""
echo "══════════════════════════════════════════════"
echo "  Telegram + Browser Automation — Setup"
echo "══════════════════════════════════════════════"
echo ""

# 1. Install Python dependencies
echo "► Installing Python packages…"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "  ✅ Python packages installed."

# 2. Install Playwright + Chromium browser
echo "► Installing Playwright Chromium browser…"
playwright install chromium
playwright install-deps chromium
echo "  ✅ Playwright + Chromium ready."

# 3. Remind user to update config
echo ""
echo "══════════════════════════════════════════════"
echo "  NEXT STEPS"
echo "══════════════════════════════════════════════"
echo ""
echo "  1. Open config.json and set:"
echo "       \"your_personal_telegram_id\": YOUR_NUMERIC_ID"
echo "     (Get it from @userinfobot on Telegram)"
echo ""
echo "  2. Authorise your userbot account (one-time only):"
echo "       python main.py auth"
echo ""
echo "  3. Start the automation:"
echo "       python main.py"
echo ""
echo "  The Control Bot will message you on Telegram."
echo "  Use the keyboard buttons to add tasks and start the loop."
echo "══════════════════════════════════════════════"
echo ""
