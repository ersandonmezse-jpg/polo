"""
Ana Başlatıcı
=============
1. Flask web paneli başlatır (Railway PORT'unda)
2. Telegram bot menü butonunu ayarlar (PUBLIC_URL ile)
3. Bot'un sheet kontrol döngüsünü başlatır
"""

import os
import sys
import time
import threading
import logging

import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WEB_PORT,
    PUBLIC_URL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_web_panel():
    """Flask web panelini başlatır."""
    from web_panel import app
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False)


def set_bot_menu_button(web_app_url: str):
    """Telegram bot'a Mini App menü butonu ekler."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setChatMenuButton"

    payload = {
        "menu_button": {
            "type": "web_app",
            "text": "📊 Panel",
            "web_app": {"url": web_app_url},
        }
    }

    try:
        response = requests.post(api_url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            logger.info(f"Telegram menu butonu ayarlandi: {web_app_url}")
        else:
            logger.error(f"Menu butonu hatasi: {data}")
    except Exception as e:
        logger.error(f"Menu butonu ayarlanamadi: {e}")

    # Chat'e ozel menu butonu
    chat_payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "menu_button": {
            "type": "web_app",
            "text": "📊 Panel",
            "web_app": {"url": web_app_url},
        }
    }
    try:
        requests.post(api_url, json=chat_payload, timeout=10)
    except:
        pass


def set_bot_commands():
    """Bot komutlarını ayarlar."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    payload = {
        "commands": [
            {"command": "panel", "description": "Admin paneli ac"},
            {"command": "durum", "description": "Bot durumunu goster"},
        ]
    }
    try:
        requests.post(api_url, json=payload, timeout=10)
        logger.info("Bot komutlari ayarlandi")
    except:
        pass


def send_panel_link(web_app_url: str):
    """Gruba panel linkini gonderir."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "🌐 <b>Admin Panel Aktif!</b>\n\n"
                "Asagidaki butona tiklayarak paneli acabilirsiniz.",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "📊 Paneli Ac",
                    "url": web_app_url
                }
            ]]
        }
    }

    try:
        response = requests.post(api_url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            logger.info("Panel linki gruba gonderildi")
        else:
            logger.warning(f"Panel linki gonderilemedi: {data}")
    except Exception as e:
        logger.warning(f"Panel linki gonderilemedi: {e}")


def run_bot():
    """Telegram bot'unu baslatir."""
    from bot import main as bot_main
    bot_main()


def main():
    # Windows konsol encoding fix
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print()
    print("  ==========================================")
    print("    Google Sheets Telegram Bot v3.0")
    print("  ==========================================")
    print()

    # 1. Web paneli baslat
    logger.info(f"Web panel baslatiliyor (port {WEB_PORT})...")
    web_thread = threading.Thread(target=run_web_panel, daemon=True)
    web_thread.start()
    time.sleep(2)
    logger.info(f"Web Panel -> http://localhost:{WEB_PORT}")

    # 2. Public URL varsa bot menu butonunu ayarla
    if PUBLIC_URL:
        logger.info(f"Public URL: {PUBLIC_URL}")
        set_bot_menu_button(PUBLIC_URL)
        set_bot_commands()
        send_panel_link(PUBLIC_URL)
    else:
        logger.warning("PUBLIC_URL ayarlanmamis. Telegram Mini App butonu olusturulmadi.")
        logger.warning("Railway'de RAILWAY_PUBLIC_DOMAIN otomatik atanir.")
        set_bot_commands()

    print()
    print(f"  Lokal:  http://localhost:{WEB_PORT}")
    if PUBLIC_URL:
        print(f"  Public: {PUBLIC_URL}")
    print()

    # 3. Bot dongusunu baslat (ana thread'de)
    run_bot()


if __name__ == "__main__":
    main()
