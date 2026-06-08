#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  —  Build + setup script for Telegram + Browser automation project
#
# Used in two ways:
#   Railway build command : bash setup.sh
#   Local first-run       : bash setup.sh
#
# Steps:
#   1. Print environment info
#   2. pip install -r requirements.txt
#   3. playwright install-deps chromium   (system libs — needs root / sudo)
#   4. playwright install chromium        (download Chromium binary)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

echo ""
echo "══════════════════════════════════════════════"
echo "  Telegram + Browser Automation — Build Setup"
echo "══════════════════════════════════════════════"
echo ""

# ── 1. Environment info ───────────────────────────────────────────────────────
echo "► Environment"
echo "  Python : $(python --version 2>&1)"
echo "  pip    : $(pip --version 2>&1 | cut -d' ' -f1-2)"
echo "  whoami : $(whoami)"
echo ""

# ── 2. Python packages ────────────────────────────────────────────────────────
echo "► Installing Python packages…"
pip install --upgrade pip --quiet --no-cache-dir
pip install -r requirements.txt --quiet --no-cache-dir
echo "  ✅ Python packages installed."
echo ""

# ── 3. Playwright system dependencies (apt packages for Chromium) ─────────────
# playwright install-deps calls apt-get internally and needs root.
# On Railway the build container runs as root; locally you may need sudo.
echo "► Installing Playwright system dependencies (Chromium apt libs)…"
if [ "$(id -u)" = "0" ]; then
    playwright install-deps chromium
else
    echo "  Not root — trying with sudo…"
    sudo playwright install-deps chromium || {
        echo "  ⚠️  sudo unavailable. System deps may already be present (Railway base image)."
        echo "  Continuing — Playwright will error at runtime if libs are missing."
    }
fi
echo "  ✅ System dependencies done."
echo ""

# ── 4. Download Chromium browser binary ───────────────────────────────────────
echo "► Downloading Playwright Chromium browser binary…"
playwright install chromium
echo "  ✅ Chromium binary ready."
echo ""

# ── 5. Local-run next steps (skipped in Railway CI — no TTY) ──────────────────
if [ -t 1 ]; then
    echo "══════════════════════════════════════════════════════════════════"
    echo "  NEXT STEPS (local setup)"
    echo "══════════════════════════════════════════════════════════════════"
    echo ""
    echo "  1. Set required environment variables (or edit config.json):"
    echo "       API_ID, API_HASH, CONTROL_BOT_TOKEN,"
    echo "       PERSONAL_TELEGRAM_ID, PROXY_HOST, PROXY_PORT,"
    echo "       PROXY_USERNAME, PROXY_PASSWORD"
    echo ""
    echo "  2. Authorise your userbot account (one-time only):"
    echo "       python main.py auth"
    echo ""
    echo "     This will print a SESSION_STRING value at the end."
    echo "     Copy it and set it as SESSION_STRING in Railway Variables."
    echo "     Without this step every Railway deploy will fail with"
    echo "     'Userbot session not authorised'."
    echo ""
    echo "  3. Add SESSION_STRING to Railway:"
    echo "       Railway project → Variables → SESSION_STRING = <paste>"
    echo ""
    echo "  4. Start the automation locally:"
    echo "       python main.py"
    echo ""
    echo "  The Control Bot will message you on Telegram."
    echo "  Use the keyboard buttons to add tasks and start the loop."
    echo "══════════════════════════════════════════════════════════════════"
    echo ""
fi

echo "► Build complete."
