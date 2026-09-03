"""
Yapılandırma
=============
Değerler ortam değişkenlerinden (Environment Variables) okunur.
Railway / Sunucu üzerinde Variables sekmesinden tanımlanır.
"""

import os

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Google Sheets (Varsayılan tamamen BOŞ. Sadece panelden veya /link ile eklenenler çalışır!) ──
GOOGLE_SHEETS = []

# ── Kontrol Aralığı ──────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "45"))  # 45 saniyede bir otomatik tarar (Canlı veri akışı)

# ── Web Panel ────────────────────────────────────────────────────────────────
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1453")
WEB_PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))

# ── Railway / Deploy URL ─────────────────────────────────────────────────────
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
if not PUBLIC_URL:
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if railway_domain:
        PUBLIC_URL = f"https://{railway_domain}"
