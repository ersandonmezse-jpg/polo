"""
Google Sheets Telegram Bot
==========================
Birden fazla Google Sheets'teki belirli kolonlardan veri çeker
ve Telegram grubuna gönderir.

Çekilen kolonlar:
  - created_time (Türkiye saatine çevrilerek gösterilir)
  - çalışma_durumu
  - t.c_numaranız
  - kullanılabilir_kart_limitiniz
  - phone_number

Her sheet için ayrı numaralandırma, 0'dan başlar.
Telegram message_id'leri saklanır (panelden silinebilmesi için).
"""

import csv
import html
import io
import re
import time
import logging
from datetime import datetime

import pytz
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CHECK_INTERVAL,
)
from data_store import (
    get_sheets,
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

# ── Sabitler ─────────────────────────────────────────────────────────────────
TURKEY_TZ = pytz.timezone("Europe/Istanbul")

TARGET_COLUMNS = [
    "created_time",
    "çalışma_durumu",
    "t.c_numaranız",
    "kullanılabilir_kart_limitiniz",
    "phone_number",
]


# ── Yardımcı Fonksiyonlar ───────────────────────────────────────────────────

def fetch_sheet_data(sheet_id: str) -> list[dict]:
    """Herkese açık Google Sheets'i CSV olarak indirir ve dict listesine çevirir."""
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    response = requests.get(csv_url, timeout=30)
    response.raise_for_status()

    content = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


def normalize_column_name(name: str) -> str:
    """Kolon adını normalize eder (küçük harf, boşluk → alt çizgi, trim)."""
    return name.strip().lower().replace(" ", "_")


def find_column_mapping(headers: list[str]) -> dict[str, str]:
    """CSV başlıklarını hedef kolon isimleriyle eşleştirir."""
    mapping = {}
    for header in headers:
        normalized = normalize_column_name(header)
        for target in TARGET_COLUMNS:
            if normalized == target or normalized.replace(".", "").replace(" ", "_") == target.replace(".", ""):
                mapping[target] = header
                break
    return mapping


def convert_to_turkey_time(time_str: str) -> str:
    """Tarih/saat stringini Türkiye saatine çevirir."""
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
    """Tek bir satır için Telegram mesaj metnini oluşturur."""
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


def send_telegram_message(text: str) -> tuple[bool, int | None]:
    """
    Telegram'a mesaj gönderir. Rate limit durumunda bekleyip tekrar dener.
    Döner: (başarılı_mı, telegram_message_id)
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=15)
            data = response.json()
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                logger.info(f"Mesaj başarıyla gönderildi (ID: {msg_id}).")
                return True, msg_id
            elif data.get("error_code") == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 30)
                logger.warning(f"Rate limit! {retry_after} saniye bekleniyor... (Deneme {attempt + 1}/{max_retries})")
                time.sleep(retry_after + 1)
            else:
                logger.error(f"Telegram API hatası: {data}")
                return False, None
        except requests.RequestException as e:
            logger.error(f"Telegram gönderim hatası: {e}")
            return False, None

    logger.error("Maksimum deneme sayısına ulaşıldı.")
    return False, None


def check_and_send_sheet(sheet_config: dict):
    """Tek bir sheet için veri çek ve yeni satırları Telegram'a gönder."""
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
        logger.info(f"[{sheet_name}] Sheet'te veri bulunamadı.")
        return

    # Kolon eşleştirmesi
    headers = list(rows[0].keys())
    col_mapping = find_column_mapping(headers)

    missing_cols = [c for c in TARGET_COLUMNS if c not in col_mapping]
    if missing_cols:
        logger.warning(f"[{sheet_name}] Eksik kolonlar: {missing_cols}")

    if not col_mapping:
        logger.error(f"[{sheet_name}] Hiçbir hedef kolon bulunamadı!")
        update_sheet_meta(sheet_id, total_rows, "Kolon hatası")
        return

    # Son gönderilen index
    last_sent = get_last_sent(sheet_id)

    if last_sent >= total_rows:
        logger.info(f"[{sheet_name}] Yeni veri yok. (Toplam: {total_rows}, Son gönderilen: {last_sent})")
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
            logger.error(f"[{sheet_name}] Satır #{entry_number} gönderilemedi. Tekrar denenecek.")
            break


def check_all_sheets():
    """Tüm aktif sheet'leri kontrol eder."""
    sheets = get_sheets()
    for sheet_config in sheets:
        if not sheet_config.get("active", True):
            logger.info(f"[{sheet_config['name']}] Pasif durumda, atlanıyor.")
            continue
        try:
            check_and_send_sheet(sheet_config)
        except Exception as e:
            logger.error(f"[{sheet_config['name']}] Beklenmeyen hata: {e}")


def main():
    """Bot ana döngüsü."""
    logger.info("=" * 50)
    logger.info("  Google Sheets → Telegram Bot Başlatıldı")
    sheets = get_sheets()
    logger.info(f"  Kayıtlı sheet sayısı: {len(sheets)}")
    logger.info(f"  Kontrol aralığı: {CHECK_INTERVAL} saniye ({CHECK_INTERVAL // 60} dakika)")
    logger.info("=" * 50)

    # İlk kontrol
    try:
        check_all_sheets()
    except Exception as e:
        logger.error(f"İlk kontrolde hata: {e}")

    # Periyodik kontrol
    while True:
        logger.info(f"{CHECK_INTERVAL} saniye bekleniyor...")
        time.sleep(CHECK_INTERVAL)
        try:
            check_all_sheets()
        except Exception as e:
            logger.error(f"Kontrol sırasında hata: {e}")


if __name__ == "__main__":
    main()
