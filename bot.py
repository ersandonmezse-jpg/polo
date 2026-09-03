"""
Google Sheets Telegram Bot
==========================
1. Belirli kolonlardan veri çeker ve Telegram grubuna gönderir.
2. Telegram üzerinden gelen komutları (/link, /start, /durum, /panel) dinler ve yanıtlar!
"""

import csv
import html
import io
import re
import time
import threading
import logging
from datetime import datetime

import pytz
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CHECK_INTERVAL,
    PUBLIC_URL,
)
from data_store import (
    get_sheets,
    add_sheet,
    get_last_sent,
    record_message_sent,
    update_sheet_meta,
    extract_sheet_id,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TURKEY_TZ = pytz.timezone("Europe/Istanbul")

TARGET_COLUMNS = [
    "created_time",
    "çalışma_durumu",
    "t.c_numaranız",
    "kullanılabilir_kart_limitiniz",
    "phone_number",
]


# ── Sheet Yardımcıları ──────────────────────────────────────────────────────

def fetch_sheet_data(sheet_id: str) -> list[dict]:
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    response = requests.get(csv_url, timeout=30)
    response.raise_for_status()

    content = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


def normalize_column_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def find_column_mapping(headers: list[str]) -> dict[str, str]:
    mapping = {}
    for header in headers:
        normalized = normalize_column_name(header)
        for target in TARGET_COLUMNS:
            if normalized == target or normalized.replace(".", "").replace(" ", "_") == target.replace(".", ""):
                mapping[target] = header
                break
    return mapping


def convert_to_turkey_time(time_str: str) -> str:
    if not time_str or not time_str.strip():
        return "Bilinmiyor"

    time_str = time_str.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = pytz.utc.localize(dt)
            dt_turkey = dt.astimezone(TURKEY_TZ)
            return dt_turkey.strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue

    return time_str


def format_message(entry_number: int, row: dict, col_mapping: dict, sheet_name: str) -> str:
    created_time_raw = row.get(col_mapping.get("created_time", ""), "")
    created_time = html.escape(convert_to_turkey_time(created_time_raw))

    calisma_durumu = html.escape(row.get(col_mapping.get("çalışma_durumu", ""), "—"))
    tc_no = html.escape(row.get(col_mapping.get("t.c_numaranız", ""), "—"))
    kart_limit = html.escape(row.get(col_mapping.get("kullanılabilir_kart_limitiniz", ""), "—"))
    phone = html.escape(row.get(col_mapping.get("phone_number", ""), "—"))

    message = (
        f"📋 <b>Kayıt #{entry_number}</b> — <i>{html.escape(sheet_name)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>Tarih:</b> {created_time}\n"
        f"💼 <b>Çalışma Durumu:</b> {calisma_durumu}\n"
        f"🆔 <b>T.C. Numarası:</b> <code>{tc_no}</code>\n"
        f"💳 <b>Kart Limiti:</b> {kart_limit}\n"
        f"📞 <b>Telefon:</b> {phone}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    return message


def send_telegram_message(text: str, chat_id: str = None, reply_markup: dict = None) -> tuple[bool, int | None]:
    if not TELEGRAM_BOT_TOKEN:
        return False, None

    target_chat = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=15)
            data = response.json()
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                return True, msg_id
            elif data.get("error_code") == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 15)
                time.sleep(retry_after + 1)
            else:
                logger.error(f"Telegram API hatası: {data}")
                return False, None
        except requests.RequestException as e:
            logger.error(f"Telegram gönderim hatası: {e}")
            return False, None

    return False, None


# ── Telegram Komut Dinleyicisi (Polling) ────────────────────────────────────

def listen_telegram_updates():
    """Telegram'dan gelen /link, /start, /durum gibi mesajları dinler."""
    if not TELEGRAM_BOT_TOKEN:
        return

    logger.info("Telegram komut dinleyicisi başlatıldı...")
    offset = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 20, "offset": offset}
            res = requests.get(url, params=params, timeout=25)
            if res.status_code != 200:
                time.sleep(5)
                continue

            data = res.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                text = (msg.get("text") or "").strip()
                from_chat_id = msg["chat"]["id"]

                # 1. /start Komutu
                if text.startswith("/start"):
                    welcome_text = (
                        "👋 <b>Merhaba! Google Sheets Takip Botu Aktif.</b>\n\n"
                        "Komutlar:\n"
                        "• <code>/link &lt;Google Sheets Linki&gt;</code> - Yeni sheet ekler\n"
                        "• <code>/durum</code> - Aktif sheetleri listeler\n"
                        "• <code>/panel</code> - Web paneli açar"
                    )
                    markup = None
                    if PUBLIC_URL:
                        markup = {"inline_keyboard": [[{"text": "📊 Web Paneli Aç", "url": PUBLIC_URL}]]}
                    send_telegram_message(welcome_text, chat_id=from_chat_id, reply_markup=markup)

                # 2. /link Komutu
                elif text.startswith("/link") or "docs.google.com/spreadsheets" in text:
                    # Linki ayıkla
                    parts = text.split(maxsplit=1)
                    target_url = parts[1].strip() if len(parts) > 1 else text.strip()

                    match = re.search(r"https?://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9-_]+[^\s]*", target_url)
                    if match:
                        clean_url = match.group(0)
                        sheet_name = f"Form {len(get_sheets()) + 1}"
                        success, msg_resp = add_sheet(sheet_name, clean_url)

                        if success:
                            reply_msg = (
                                f"✅ <b>Google Sheets Başarıyla Eklendi!</b>\n\n"
                                f"📝 <b>İsim:</b> {sheet_name}\n"
                                f"🔗 <b>Link:</b> {clean_url}\n\n"
                                f"Bot bu tablodaki yeni kayıtları da otomatik olarak gönderecektir."
                            )
                        else:
                            reply_msg = f"ℹ️ {msg_resp}"
                    else:
                        reply_msg = (
                            "⚠️ <b>Geçerli bir link bulunamadı!</b>\n\n"
                            "Kullanım:\n"
                            "<code>/link https://docs.google.com/spreadsheets/d/.../edit</code>"
                        )
                    send_telegram_message(reply_msg, chat_id=from_chat_id)

                # 3. /durum Komutu
                elif text.startswith("/durum"):
                    sheets = get_sheets()
                    sheet_lines = []
                    for idx, s in enumerate(sheets, 1):
                        status_emoji = "🟢" if s.get("active", True) else "⚪"
                        sheet_lines.append(f"{idx}. {status_emoji} <b>{html.escape(s['name'])}</b> ({s.get('count', 0)} kayıt)")

                    status_msg = (
                        f"📊 <b>Bot Durumu:</b>\n\n"
                        f"Kayıtlı Sheet Sayısı: {len(sheets)}\n"
                        + "\n".join(sheet_lines)
                    )
                    send_telegram_message(status_msg, chat_id=from_chat_id)

                # 4. /panel Komutu
                elif text.startswith("/panel"):
                    if PUBLIC_URL:
                        markup = {"inline_keyboard": [[{"text": "📊 Paneli Aç", "url": PUBLIC_URL}]]}
                        send_telegram_message("Aşağıdaki butona tıklayarak web panele giriş yapabilirsiniz:", chat_id=from_chat_id, reply_markup=markup)
                    else:
                        send_telegram_message("Panel adresi henüz ayarlanmamış.", chat_id=from_chat_id)

        except Exception as e:
            logger.debug(f"Update dinleme hatası: {e}")
            time.sleep(3)


# ── Sheet Kontrol Döngüsü ───────────────────────────────────────────────────

def check_and_send_sheet(sheet_config: dict):
    sheet_name = sheet_config["name"]
    sheet_url = sheet_config["url"]
    sheet_id = sheet_config.get("id") or extract_sheet_id(sheet_url)

    logger.info(f"── Sheet kontrol ediliyor: {sheet_name} ──")

    try:
        rows = fetch_sheet_data(sheet_id)
    except Exception as e:
        logger.error(f"[{sheet_name}] Veri çekilemedi: {e}")
        update_sheet_meta(sheet_id, 0, f"Hata: {str(e)[:35]}")
        return

    total_rows = len(rows)
    update_sheet_meta(sheet_id, total_rows, "Aktif")

    if not rows:
        return

    headers = list(rows[0].keys())
    col_mapping = find_column_mapping(headers)

    if not col_mapping:
        logger.error(f"[{sheet_name}] Hedef kolon bulunamadı!")
        update_sheet_meta(sheet_id, total_rows, "Kolon hatası")
        return

    last_sent = get_last_sent(sheet_id)
    if last_sent >= total_rows:
        return

    new_rows = rows[last_sent:]
    logger.info(f"[{sheet_name}] {len(new_rows)} yeni satır bulundu. Gönderiliyor...")

    for i, row in enumerate(new_rows):
        entry_number = last_sent + i
        message = format_message(entry_number, row, col_mapping, sheet_name)

        success, msg_id = send_telegram_message(message)
        if success:
            record_message_sent(sheet_id, entry_number, msg_id or 0)
            time.sleep(2)
        else:
            logger.error(f"[{sheet_name}] Satır #{entry_number} gönderilemedi.")
            break


def check_all_sheets():
    sheets = get_sheets()
    for sheet_config in sheets:
        if not sheet_config.get("active", True):
            continue
        try:
            check_and_send_sheet(sheet_config)
        except Exception as e:
            logger.error(f"[{sheet_config['name']}] Hata: {e}")


def main():
    logger.info("=" * 50)
    logger.info("  Google Sheets → Telegram Bot Başlatıldı")
    sheets = get_sheets()
    logger.info(f"  Kayıtlı sheet sayısı: {len(sheets)}")
    logger.info(f"  Kontrol aralığı: {CHECK_INTERVAL} saniye ({CHECK_INTERVAL // 60} dakika)")
    logger.info("=" * 50)

    # Telegram komut dinleyicisini ayrı bir thread'de başlat
    listener_thread = threading.Thread(target=listen_telegram_updates, daemon=True)
    listener_thread.start()

    # İlk kontrol
    try:
        check_all_sheets()
    except Exception as e:
        logger.error(f"İlk kontrolde hata: {e}")

    # Periyodik kontrol döngüsü
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            check_all_sheets()
        except Exception as e:
            logger.error(f"Kontrol sırasında hata: {e}")


if __name__ == "__main__":
    main()
