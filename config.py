"""
Yapılandırma
=============
Yerel geliştirmede bu dosyadaki değerler kullanılır.
Railway'de (veya başka bir sunucuda) ortam değişkenlerinden okunur.
"""

import os

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8860660735:AAFu0gmU_kwBGaETIyhJZ-yXD-0SSOuiB44"
)

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "-5529859923"
)

# ── Google Sheets (varsayılan linkler — panel üzerinden de eklenebilir) ──────
GOOGLE_SHEETS = [
    {
        "name": "Ana Form",
        "url": "https://docs.google.com/spreadsheets/d/1JrqIsJZ7dnY3RXQFfQS1NGIGqcXDP_bHwbJjleEpE0s/edit?usp=sharing",
    },
]

# ── Kontrol Aralığı ──────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "1200"))  # 20 dakika

# ── Web Panel ────────────────────────────────────────────────────────────────
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1453")
WEB_PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "5000")))

# ── Railway / Deploy URL ─────────────────────────────────────────────────────
# Railway otomatik RAILWAY_PUBLIC_DOMAIN verir. Manuel de ayarlanabilir.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
if not PUBLIC_URL:
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if railway_domain:
        PUBLIC_URL = f"https://{railway_domain}"
