"""
Yapılandırma
=============
Değerler ortam değişkenlerinden (Environment Variables) okunur.
Railway / Sunucu üzerinde Variables sekmesinden tanımlanır.
"""

import os

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or "8860660735:AAFu0gmU_kwBGaETIyhJZ-yXD-0SSOuiB44")
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID", "").strip() or "-5529859923")

# ── Google Sheets (Varsayılan) ──
GOOGLE_SHEETS = [
    {
        "name": "zigiligo",
        "url": "https://docs.google.com/spreadsheets/d/1dlvi5pO1OZ4Z5DhXcfmE38tIBGdFwjfM0-ITpWWYw0k/edit?usp=sharing",
        "chat_id": "-5529859923"
    }
]

# ── Kontrol Aralığı ──────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "45"))  # 45 saniyede bir otomatik tarar (Canlı veri akışı)

# ── Web Panel ────────────────────────────────────────────────────────────────
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1453")
WEB_PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))

# ── Railway / Deploy URL ─────────────────────────────────────────────────────
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()
if not PUBLIC_URL:
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        PUBLIC_URL = f"https://{railway_domain}"

if PUBLIC_URL and not PUBLIC_URL.startswith(("http://", "https://")):
    PUBLIC_URL = f"https://{PUBLIC_URL}"

