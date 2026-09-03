"""
Google Sheets Telegram Bot
==========================
1. Belirli kolonlardan veri çeker ve Telegram grubuna etkileşim butonlarıyla gönderir:
   [ 🟢 Olumlu ] [ 🔴 Olumsuz ]
   [ 💳 Kredi Düştü ] [ 📵 Cevapsız ]
   [ ↗️ Gruba Aktar ]
2. Buton tıklamalarını (Callback Query) dinler:
   - Durum seçildiğinde mesajı günceller (Kim, ne zaman seçti)
   - "Gruba Aktar" denildiğinde hedef grup seçim & onay adımlarını yönetir
3. Telegram üzerinden gelen komutları (/link, /grup_ekle, /start, /durum, /panel) dinler
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
    get_groups,
    add_group,
    set_record_status,
    get_record_status,
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


def format_message(entry_number: int, row: dict, col_mapping: dict, sheet_name: str, status_note: str = "") -> str:
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
    if status_note:
        message += f"\n{status_note}"

    return message


def build_record_keyboard(sheet_id: str, row_num: int) -> dict:
    """Kayıt altına yerleştirilecek standart etkileşim butonları."""
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 Olumlu", "callback_data": f"st:{sheet_id}:{row_num}:olumlu"},
                {"text": "🔴 Olumsuz", "callback_data": f"st:{sheet_id}:{row_num}:olumsuz"}
            ],
            [
                {"text": "💳 Kredi Düştü", "callback_data": f"st:{sheet_id}:{row_num}:kredi"},
                {"text": "📵 Cevapsız", "callback_data": f"st:{sheet_id}:{row_num}:cevapsiz"}
            ],
            [
                {"text": "↗️ Gruba Aktar", "callback_data": f"fwd_menu:{sheet_id}:{row_num}"}
            ]
        ]
    }


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


def edit_telegram_message(chat_id: int | str, message_id: int, text: str, reply_markup: dict = None):
    """Mevcut bir Telegram mesajının metnini ve butonlarını günceller."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Mesaj düzenleme hatası: {e}")


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False):
    """Butona tıklandığında Telegram bildirimini kapatır/alert gösterir."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert
        }, timeout=8)
    except Exception:
        pass


# ── Telegram Güncelleme Dinleyicisi (Komutlar & Buton Tıklamaları) ─────────

STATUS_MAP = {
    "olumlu": "🟢 Olumlu",
    "olumsuz": "🔴 Olumsuz",
    "kredi": "💳 Kredi Düştü",
    "cevapsiz": "📵 Cevapsız",
}


def listen_telegram_updates():
    if not TELEGRAM_BOT_TOKEN:
        return

    logger.info("Telegram komut ve buton dinleyicisi başlatıldı...")
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

                # ── 1. BUTON TIKLAMALARI (Callback Query) ───────────────────
                if "callback_query" in update:
                    cq = update["callback_query"]
                    cq_id = cq["id"]
                    cq_data = cq.get("data", "")
                    from_user = cq.get("from", {})
                    user_tag = f"@{from_user.get('username')}" if from_user.get("username") else (from_user.get("first_name") or "Temsilci")
                    msg = cq.get("message")
                    if not msg:
                        continue
                    chat_id = msg["chat"]["id"]
                    message_id = msg["message_id"]
                    original_text = msg.get("text", "")

                    # A) Durum Değişikliği (st:sheet_id:row_num:status)
                    if cq_data.startswith("st:"):
                        parts = cq_data.split(":")
                        if len(parts) >= 4:
                            sheet_id = parts[1]
                            row_num = int(parts[2])
                            st_key = parts[3]
                            status_label = STATUS_MAP.get(st_key, st_key)

                            # Veritabanında güncelle
                            set_record_status(sheet_id, row_num, status_label, user_tag)

                            # Mesajın altına not ekle
                            base_text = original_text.split("\n📌 Durum:")[0].split("\n↪️")[0]
                            now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                            new_text = (
                                f"{html.escape(base_text)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📌 <b>Durum:</b> {status_label} ({html.escape(user_tag)} - {now_time})"
                            )

                            answer_callback_query(cq_id, f"Seçildi: {status_label}")
                            edit_telegram_message(chat_id, message_id, new_text, reply_markup=build_record_keyboard(sheet_id, row_num))

                    # B) Gruba Aktar Menüsü (fwd_menu:sheet_id:row_num)
                    elif cq_data.startswith("fwd_menu:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        groups = get_groups()

                        # Hedef grupları buton olarak diz
                        group_buttons = []
                        for g in groups:
                            # Aynı gruba tekrar aktarmayı engelle veya hepsini listele
                            group_buttons.append([{"text": f"👥 {g['name']}", "callback_data": f"fwd_sel:{sheet_id}:{row_num}:{g['id']}"}])

                        group_buttons.append([{"text": "❌ İptal", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}])

                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + "\n\n<i>↪️ Hangi gruba aktarmak istiyorsunuz?</i>",
                            reply_markup={"inline_keyboard": group_buttons}
                        )

                    # C) Hedef Grup Seçildi -> Onay Adımı (fwd_sel:sheet_id:row_num:target_chat_id)
                    elif cq_data.startswith("fwd_sel:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        target_chat_id = parts[3]

                        groups = get_groups()
                        target_name = next((g["name"] for g in groups if str(g["id"]) == target_chat_id), "Seçilen Grup")

                        confirm_keyboard = {
                            "inline_keyboard": [
                                [
                                    {"text": "✅ Evet, Gruba Gönder", "callback_data": f"fwd_do:{sheet_id}:{row_num}:{target_chat_id}"}
                                ],
                                [
                                    {"text": "❌ Vazgeç", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}
                                ]
                            ]
                        }
                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text).replace("\n\n<i>↪️ Hangi gruba aktarmak istiyorsunuz?</i>", "") +
                            f"\n\n❓ <b>Bu kayıt '{html.escape(target_name)}' grubuna aktarılsın mı?</b>",
                            reply_markup=confirm_keyboard
                        )

                    # D) Aktarmayı Onayla (fwd_do:sheet_id:row_num:target_chat_id)
                    elif cq_data.startswith("fwd_do:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        target_chat_id = parts[3]

                        groups = get_groups()
                        target_name = next((g["name"] for g in groups if str(g["id"]) == target_chat_id), "Hedef Grup")

                        # Temiz kayıt metnini hazırla
                        clean_lead_text = original_text.split("\n\n❓")[0].split("\n\n<i>↪️")[0]

                        # Hedef gruba butonlarla birlikte yolla
                        send_telegram_message(
                            html.escape(clean_lead_text) + f"\n\n<i>(Aktaran: {html.escape(user_tag)})</i>",
                            chat_id=target_chat_id,
                            reply_markup=build_record_keyboard(sheet_id, row_num)
                        )

                        # Orijinal gruptaki mesaja aktarıldı notu ekle
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        updated_orig = (
                            f"{html.escape(clean_lead_text)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"↪️ <b>{html.escape(target_name)}</b> grubuna aktarıldı ({html.escape(user_tag)} - {now_time})"
                        )
                        answer_callback_query(cq_id, f"Kayıt {target_name} grubuna aktarıldı! ✅", show_alert=True)
                        edit_telegram_message(chat_id, message_id, updated_orig, reply_markup=build_record_keyboard(sheet_id, row_num))

                    # E) İptal Et (fwd_cancel:sheet_id:row_num)
                    elif cq_data.startswith("fwd_cancel:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        clean_text = original_text.split("\n\n❓")[0].split("\n\n<i>↪️")[0]
                        answer_callback_query(cq_id, "İşlem iptal edildi.")
                        edit_telegram_message(chat_id, message_id, html.escape(clean_text), reply_markup=build_record_keyboard(sheet_id, row_num))

                # ── 2. METİN KOMUTLARI ─────────────────────────────────────
                elif "message" in update:
                    msg = update["message"]
                    text = (msg.get("text") or "").strip()
                    from_chat_id = msg["chat"]["id"]

                    # /start Komutu
                    if text.startswith("/start"):
                        welcome_text = (
                            "👋 <b>Google Sheets Lead Takip Botu Aktif!</b>\n\n"
                            "Komutlar:\n"
                            "• <code>/link &lt;Link&gt;</code> - Yeni Google Sheets ekler\n"
                            "• <code>/grup_ekle &lt;Grup Adı&gt;</code> - Bu grubu aktarım listesine ekler\n"
                            "• <code>/durum</code> - Sheet ve grup listesini gösterir\n"
                            "• <code>/panel</code> - Web paneli açar"
                        )
                        markup = None
                        if PUBLIC_URL:
                            markup = {"inline_keyboard": [[{"text": "📊 Web Paneli Aç", "url": PUBLIC_URL}]]}
                        send_telegram_message(welcome_text, chat_id=from_chat_id, reply_markup=markup)

                    # /grup_ekle Komutu (O an yazılan grubu aktarım listesine kaydeder)
                    elif text.startswith("/grup_ekle"):
                        parts = text.split(maxsplit=1)
                        g_name = parts[1].strip() if len(parts) > 1 else (msg["chat"].get("title") or "Yeni Grup")
                        success, resp_msg = add_group(g_name, str(from_chat_id))
                        if success:
                            reply = f"✅ <b>Grup Başarıyla Eklendi!</b>\n\nİsim: <b>{html.escape(g_name)}</b>\nID: <code>{from_chat_id}</code>\n\nArtık kayıtları bu gruba aktarabilirsiniz."
                        else:
                            reply = f"ℹ️ {resp_msg}"
                        send_telegram_message(reply, chat_id=from_chat_id)

                    # /link Komutu
                    elif text.startswith("/link") or "docs.google.com/spreadsheets" in text:
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
                                    f"Yeni kayıtlar etkileşim butonlarıyla birlikte gönderilecektir."
                                )
                            else:
                                reply_msg = f"ℹ️ {msg_resp}"
                        else:
                            reply_msg = "⚠️ <b>Geçerli bir link bulunamadı!</b>\nKullanım: <code>/link https://docs.google.com/...</code>"
                        send_telegram_message(reply_msg, chat_id=from_chat_id)

                    # /durum Komutu
                    elif text.startswith("/durum"):
                        sheets = get_sheets()
                        groups = get_groups()
                        lines = [f"📊 <b>Kayıtlı Sheet Sayısı:</b> {len(sheets)}"]
                        for idx, s in enumerate(sheets, 1):
                            emoji = "🟢" if s.get("active", True) else "⚪"
                            lines.append(f"{idx}. {emoji} <b>{html.escape(s['name'])}</b> ({s.get('count', 0)} kayıt)")

                        lines.append(f"\n👥 <b>Hedef Gruplar:</b> {len(groups)}")
                        for idx, g in enumerate(groups, 1):
                            lines.append(f"{idx}. 👥 <b>{html.escape(g['name'])}</b> (<code>{g['id']}</code>)")

                        send_telegram_message("\n".join(lines), chat_id=from_chat_id)

                    # /panel Komutu
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
    logger.info(f"[{sheet_name}] {len(new_rows)} yeni satır bulundu. Butonlu gönderiliyor...")

    for i, row in enumerate(new_rows):
        entry_number = last_sent + i
        message = format_message(entry_number, row, col_mapping, sheet_name)
        keyboard = build_record_keyboard(sheet_id, entry_number)

        success, msg_id = send_telegram_message(message, reply_markup=keyboard)
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
    logger.info("  Google Sheets → Telegram Bot (Etkileşim Butonlu) Başlatıldı")
    sheets = get_sheets()
    logger.info(f"  Kayıtlı sheet sayısı: {len(sheets)}")
    logger.info(f"  Kontrol aralığı: {CHECK_INTERVAL} saniye ({CHECK_INTERVAL // 60} dakika)")
    logger.info("=" * 50)

    # Telegram buton ve komut dinleyicisini ayrı thread'de başlat
    listener_thread = threading.Thread(target=listen_telegram_updates, daemon=True)
    listener_thread.start()

    # İlk kontrol
    try:
        check_all_sheets()
    except Exception as e:
        logger.error(f"İlk kontrolde hata: {e}")

    # Periyodik döngü
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            check_all_sheets()
        except Exception as e:
            logger.error(f"Kontrol sırasında hata: {e}")


if __name__ == "__main__":
    main()
