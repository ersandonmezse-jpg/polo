"""
Google Sheets Telegram Bot
==========================
1. Belirli kolonlardan veri çeker ve Telegram grubuna etkileşim butonlarıyla gönderir:
   [ 📝 Not Ekle ]   [ 🔴 Olumsuz ]
   [ 💳 Kredi Düştü ] [ 📵 Cevapsız ]
   [ ↗️ Gruba Aktar ]

2. Buton Tıklamaları ve Saha Operasyon Akışı:
   - "📝 Not Ekle": Temsilciden not girmesini ister, mesaja iliştirir.
   - "💳 Kredi Düştü": Tutar seçim butonları çıkartır (5.000, 10.000, 20.000, 50.000, Özel).
     -> Tutar onaylandığında SAHA GRUBUNA mesaj gider, /sahaci rolündeki kişiler etiketlenir.
     -> Sahacı mesaja yanıt verip IBAN paylaşınca veya butona basınca, İLK GRUBA otomatik IBAN bilgisi iletilir!
   - "↗️ Gruba Aktar": Hedef grup seçimi ve onay adımı ile aktarır.

3. Telegram Komutları:
   - /sahaci veya /sahaciyim : Kişiyi sahacı rolüne kaydeder.
   - /saha_grubu : Bulunulan grubu saha grubu olarak kaydeder.
   - /grup_ekle <Grup Adı> : Grubu aktarım listesine ekler.
   - /link <Sheets URL> : Yeni Google Sheets ekler.
   - /durum : Sheet ve grup listesini gösterir.
   - /panel : Web paneli açar.
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
    get_settings,
    set_saha_group,
    add_sahaci_user,
    remove_sahaci_user,
    set_record_status,
    get_record_status,
    set_pending_action,
    get_pending_action,
    clear_pending_action,
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
                {"text": "📝 Not Ekle", "callback_data": f"note_req:{sheet_id}:{row_num}"},
                {"text": "🔴 Olumsuz", "callback_data": f"st:{sheet_id}:{row_num}:olumsuz"}
            ],
            [
                {"text": "💳 Kredi Düştü", "callback_data": f"kredi_req:{sheet_id}:{row_num}"},
                {"text": "📵 Cevapsız", "callback_data": f"st:{sheet_id}:{row_num}:cevapsiz"}
            ],
            [
                {"text": "↗️ Gruba Aktar", "callback_data": f"fwd_menu:{sheet_id}:{row_num}"}
            ]
        ]
    }


def send_telegram_message(text: str, chat_id: str = None, reply_markup: dict = None, reply_to_message_id: int = None) -> tuple[bool, int | None]:
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
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

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


STATUS_MAP = {
    "olumsuz": "🔴 Olumsuz",
    "cevapsiz": "📵 Cevapsız",
}


# ── Telegram Güncelleme Dinleyicisi (Komutlar & Buton Tıklamaları) ─────────

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
                    user_id = from_user.get("id")
                    user_tag = f"@{from_user.get('username')}" if from_user.get("username") else (from_user.get("first_name") or "Temsilci")
                    msg = cq.get("message")
                    if not msg:
                        continue
                    chat_id = msg["chat"]["id"]
                    message_id = msg["message_id"]
                    original_text = msg.get("text", "")

                    # 1.1) Standart Durum Değişikliği (Olumsuz, Cevapsız)
                    if cq_data.startswith("st:"):
                        parts = cq_data.split(":")
                        if len(parts) >= 4:
                            sheet_id = parts[1]
                            row_num = int(parts[2])
                            st_key = parts[3]
                            status_label = STATUS_MAP.get(st_key, st_key)

                            set_record_status(sheet_id, row_num, status_label, user_tag)

                            base_text = original_text.split("\n📌 Durum:")[0].split("\n📝 Not:")[0].split("\n↪️")[0]
                            now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                            new_text = (
                                f"{html.escape(base_text)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📌 <b>Durum:</b> {status_label} ({html.escape(user_tag)} - {now_time})"
                            )

                            answer_callback_query(cq_id, f"Seçildi: {status_label}")
                            edit_telegram_message(chat_id, message_id, new_text, reply_markup=build_record_keyboard(sheet_id, row_num))

                    # 1.2) "📝 Not Ekle" Tıklandı
                    elif cq_data.startswith("note_req:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])

                        set_pending_action(user_id, {
                            "action": "note",
                            "sheet_id": sheet_id,
                            "row_num": row_num,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "original_text": original_text,
                        })

                        answer_callback_query(cq_id, "Lütfen eklemek istediğiniz notu mesaja yanıtlayarak veya doğrudan gruba yazın.", show_alert=True)
                        send_telegram_message(
                            f"✍️ {user_tag}, <b>Kayıt #{row_num}</b> için notunuzu yazıp bu mesaja yanıtlayın:",
                            chat_id=chat_id,
                            reply_to_message_id=message_id
                        )

                    # 1.3) "💳 Kredi Düştü" Tıklandı -> Tutar Seçim Butonları
                    elif cq_data.startswith("kredi_req:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])

                        amount_keyboard = {
                            "inline_keyboard": [
                                [
                                    {"text": "5.000 TL", "callback_data": f"kredi_amt:{sheet_id}:{row_num}:5.000 TL"},
                                    {"text": "10.000 TL", "callback_data": f"kredi_amt:{sheet_id}:{row_num}:10.000 TL"}
                                ],
                                [
                                    {"text": "20.000 TL", "callback_data": f"kredi_amt:{sheet_id}:{row_num}:20.000 TL"},
                                    {"text": "50.000 TL", "callback_data": f"kredi_amt:{sheet_id}:{row_num}:50.000 TL"}
                                ],
                                [
                                    {"text": "✍️ Diğer Tutar", "callback_data": f"kredi_custom:{sheet_id}:{row_num}"},
                                    {"text": "❌ İptal", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}
                                ]
                            ]
                        }
                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + "\n\n<i>💳 Onaylanan kredi tutarını seçin:</i>",
                            reply_markup=amount_keyboard
                        )

                    # 1.4) Diğer Tutar Yazmak İstedi
                    elif cq_data.startswith("kredi_custom:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])

                        set_pending_action(user_id, {
                            "action": "amount",
                            "sheet_id": sheet_id,
                            "row_num": row_num,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "original_text": original_text,
                        })

                        answer_callback_query(cq_id)
                        send_telegram_message(
                            f"✍️ {user_tag}, <b>Kayıt #{row_num}</b> için onaylanan tutarı yazın (Örn: <code>35.000 TL</code>):",
                            chat_id=chat_id,
                            reply_to_message_id=message_id
                        )

                    # 1.5) Tutar Seçildi -> Durumu Güncelle ve SAHA GRUBUNA BİLDİRİM AT
                    elif cq_data.startswith("kredi_amt:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        amount_str = parts[3]

                        status_label = f"💳 Kredi Düştü: {amount_str}"
                        set_record_status(sheet_id, row_num, status_label, user_tag)

                        base_text = original_text.split("\n\n<i>💳")[0].split("\n📌 Durum:")[0].split("\n↪️")[0]
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        new_text = (
                            f"{html.escape(base_text)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>Durum:</b> {status_label} ({html.escape(user_tag)} - {now_time})"
                        )

                        answer_callback_query(cq_id, f"Kredi kaydedildi: {amount_str}! Saha grubuna iletiliyor...")
                        edit_telegram_message(chat_id, message_id, new_text, reply_markup=build_record_keyboard(sheet_id, row_num))

                        # SAHA GRUBUNA BİLDİRİM
                        settings = get_settings()
                        saha_chat_id = settings.get("saha_group_id")
                        sahaci_tags = " ".join(settings.get("sahaci_users", [])) or "Saha Ekibi"

                        if saha_chat_id:
                            saha_msg = (
                                f"🔔 <b>YENİ KREDİ ONAYLANDI - SAHA BİLGİLENDİRME</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👥 <b>İlgili Sahacılar:</b> {sahaci_tags}\n"
                                f"💰 <b>Onaylanan Tutar:</b> <code>{amount_str}</code>\n"
                                f"👤 <b>Temsilci:</b> {html.escape(user_tag)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"{html.escape(base_text)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👉 <i>Lütfen aşağıdaki butona basarak veya bu mesaja yanıtlayarak IBAN paylaşınız.</i>"
                            )
                            iban_keyboard = {
                                "inline_keyboard": [
                                    [{"text": "🏦 IBAN Paylaş", "callback_data": f"iban_req:{sheet_id}:{row_num}:{chat_id}:{message_id}:{amount_str}"}]
                                ]
                            }
                            send_telegram_message(saha_msg, chat_id=saha_chat_id, reply_markup=iban_keyboard)

                    # 1.6) Sahacı "🏦 IBAN Paylaş" Butonuna Bastı
                    elif cq_data.startswith("iban_req:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        origin_chat_id = parts[3]
                        origin_msg_id = int(parts[4])
                        amount_str = parts[5]

                        set_pending_action(user_id, {
                            "action": "iban",
                            "sheet_id": sheet_id,
                            "row_num": row_num,
                            "origin_chat_id": origin_chat_id,
                            "origin_msg_id": origin_msg_id,
                            "amount_str": amount_str,
                            "saha_chat_id": chat_id,
                            "saha_msg_id": message_id,
                        })

                        answer_callback_query(cq_id)
                        send_telegram_message(
                            f"🏦 {user_tag}, lütfen <b>Kayıt #{row_num}</b> ({amount_str}) için <b>IBAN ve Alıcı Adı</b> yazıp gönderin:",
                            chat_id=chat_id,
                            reply_to_message_id=message_id
                        )

                    # 1.7) Gruba Aktar Menüsü
                    elif cq_data.startswith("fwd_menu:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        groups = get_groups()

                        group_buttons = []
                        for g in groups:
                            group_buttons.append([{"text": f"👥 {g['name']}", "callback_data": f"fwd_sel:{sheet_id}:{row_num}:{g['id']}"}])

                        group_buttons.append([{"text": "❌ İptal", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}])

                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + "\n\n<i>↪️ Hangi gruba aktarmak istiyorsunuz?</i>",
                            reply_markup={"inline_keyboard": group_buttons}
                        )

                    # 1.8) Hedef Grup Seçildi -> Onay Adımı
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

                    # 1.9) Aktarmayı Onayla
                    elif cq_data.startswith("fwd_do:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        target_chat_id = parts[3]

                        groups = get_groups()
                        target_name = next((g["name"] for g in groups if str(g["id"]) == target_chat_id), "Hedef Grup")

                        clean_lead_text = original_text.split("\n\n❓")[0].split("\n\n<i>↪️")[0]

                        send_telegram_message(
                            html.escape(clean_lead_text) + f"\n\n<i>(Aktaran: {html.escape(user_tag)})</i>",
                            chat_id=target_chat_id,
                            reply_markup=build_record_keyboard(sheet_id, row_num)
                        )

                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        updated_orig = (
                            f"{html.escape(clean_lead_text)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"↪️ <b>{html.escape(target_name)}</b> grubuna aktarıldı ({html.escape(user_tag)} - {now_time})"
                        )
                        answer_callback_query(cq_id, f"Kayıt {target_name} grubuna aktarıldı! ✅", show_alert=True)
                        edit_telegram_message(chat_id, message_id, updated_orig, reply_markup=build_record_keyboard(sheet_id, row_num))

                    # 1.10) İptal Et
                    elif cq_data.startswith("fwd_cancel:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        clean_text = original_text.split("\n\n❓")[0].split("\n\n<i>↪️")[0].split("\n\n<i>💳")[0]
                        answer_callback_query(cq_id, "İşlem iptal edildi.")
                        edit_telegram_message(chat_id, message_id, html.escape(clean_text), reply_markup=build_record_keyboard(sheet_id, row_num))

                # ── 2. METİN MESAJLARI & KOMUTLAR ──────────────────────────
                elif "message" in update:
                    msg = update["message"]
                    text = (msg.get("text") or "").strip()
                    from_user = msg.get("from", {})
                    user_id = from_user.get("id")
                    user_tag = f"@{from_user.get('username')}" if from_user.get("username") else (from_user.get("first_name") or "Temsilci")
                    from_chat_id = msg["chat"]["id"]

                    # 2.A) BEKLEYEN YANITLAR (Not, Özel Tutar, Sahacı IBAN)
                    pending = get_pending_action(user_id)

                    # Eğer bu kişi bir not yazıyorsa
                    if pending and pending.get("action") == "note":
                        sheet_id = pending["sheet_id"]
                        row_num = pending["row_num"]
                        target_chat_id = pending["chat_id"]
                        target_msg_id = pending["message_id"]
                        orig_text = pending["original_text"]

                        note_str = f"📝 Not: {text}"
                        set_record_status(sheet_id, row_num, note_str, user_tag)

                        base_text = orig_text.split("\n📝 Not:")[0].split("\n↪️")[0]
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        new_text = (
                            f"{html.escape(base_text)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📝 <b>Not:</b> {html.escape(text)} ({html.escape(user_tag)} - {now_time})"
                        )
                        edit_telegram_message(target_chat_id, target_msg_id, new_text, reply_markup=build_record_keyboard(sheet_id, row_num))
                        clear_pending_action(user_id)
                        send_telegram_message(f"✅ Not başarıyla kaydedildi.", chat_id=from_chat_id)
                        continue

                    # Eğer özel tutar yazıyorsa
                    elif pending and pending.get("action") == "amount":
                        sheet_id = pending["sheet_id"]
                        row_num = pending["row_num"]
                        target_chat_id = pending["chat_id"]
                        target_msg_id = pending["message_id"]
                        orig_text = pending["original_text"]
                        amount_str = text if "TL" in text.upper() else f"{text} TL"

                        status_label = f"💳 Kredi Düştü: {amount_str}"
                        set_record_status(sheet_id, row_num, status_label, user_tag)

                        base_text = orig_text.split("\n\n<i>💳")[0].split("\n📌 Durum:")[0].split("\n↪️")[0]
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        new_text = (
                            f"{html.escape(base_text)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 <b>Durum:</b> {status_label} ({html.escape(user_tag)} - {now_time})"
                        )
                        edit_telegram_message(target_chat_id, target_msg_id, new_text, reply_markup=build_record_keyboard(sheet_id, row_num))
                        clear_pending_action(user_id)
                        send_telegram_message(f"✅ Kredi tutarı ({amount_str}) kaydedildi ve saha grubuna yönlendiriliyor.", chat_id=from_chat_id)

                        # SAHA GRUBUNA BİLDİRİM
                        settings = get_settings()
                        saha_chat_id = settings.get("saha_group_id")
                        sahaci_tags = " ".join(settings.get("sahaci_users", [])) or "Saha Ekibi"

                        if saha_chat_id:
                            saha_msg = (
                                f"🔔 <b>YENİ KREDİ ONAYLANDI - SAHA BİLGİLENDİRME</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👥 <b>İlgili Sahacılar:</b> {sahaci_tags}\n"
                                f"💰 <b>Onaylanan Tutar:</b> <code>{amount_str}</code>\n"
                                f"👤 <b>Temsilci:</b> {html.escape(user_tag)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"{html.escape(base_text)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👉 <i>Lütfen aşağıdaki butona basarak IBAN paylaşınız.</i>"
                            )
                            iban_keyboard = {
                                "inline_keyboard": [
                                    [{"text": "🏦 IBAN Paylaş", "callback_data": f"iban_req:{sheet_id}:{row_num}:{target_chat_id}:{target_msg_id}:{amount_str}"}]
                                ]
                            }
                            send_telegram_message(saha_msg, chat_id=saha_chat_id, reply_markup=iban_keyboard)
                        continue

                    # Eğer sahacı IBAN yazıyorsa -> İLK GRUBA İLET
                    elif pending and pending.get("action") == "iban":
                        sheet_id = pending["sheet_id"]
                        row_num = pending["row_num"]
                        origin_chat_id = pending["origin_chat_id"]
                        origin_msg_id = pending["origin_msg_id"]
                        amount_str = pending["amount_str"]

                        iban_info = text
                        clear_pending_action(user_id)

                        # Sahacıya onay ver
                        send_telegram_message("✅ IBAN bilgisi temsilci grubuna iletildi.", chat_id=from_chat_id)

                        # İLK ORİJİNAL GRUBA MESAJ AT
                        orig_notify = (
                            f"💳 <b>KREDİ ONAYI İÇİN IBAN BİLGİSİ GELDİ!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📋 <b>Kayıt:</b> #{row_num}\n"
                            f"💰 <b>Tutar:</b> {amount_str}\n"
                            f"👤 <b>Sahacı:</b> {html.escape(user_tag)}\n"
                            f"🏦 <b>IBAN / Bilgiler:</b>\n"
                            f"<code>{html.escape(iban_info)}</code>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━"
                        )
                        send_telegram_message(orig_notify, chat_id=origin_chat_id, reply_to_message_id=origin_msg_id)
                        continue

                    # 2.B) STANDART KOMUTLAR

                    # /start Komutu
                    if text.startswith("/start"):
                        welcome_text = (
                            "👋 <b>Google Sheets Saha & Finans Botu Aktif!</b>\n\n"
                            "Komutlar:\n"
                            "• <code>/saha_grubu</code> - Bulunulan grubu Saha Grubu yapar\n"
                            "• <code>/sahaci</code> veya <code>/sahaciyim</code> - Sizi sahacı listesine ekler\n"
                            "• <code>/grup_ekle &lt;Grup Adı&gt;</code> - Grubu aktarım listesine ekler\n"
                            "• <code>/link &lt;Link&gt;</code> - Yeni Google Sheets ekler\n"
                            "• <code>/durum</code> - Tüm grupları ve ayarları gösterir\n"
                            "• <code>/panel</code> - Web paneli açar"
                        )
                        markup = None
                        if PUBLIC_URL:
                            markup = {"inline_keyboard": [[{"text": "📊 Web Paneli Aç", "url": PUBLIC_URL}]]}
                        send_telegram_message(welcome_text, chat_id=from_chat_id, reply_markup=markup)

                    # /saha_grubu Komutu
                    elif text.startswith("/saha_grubu"):
                        g_title = msg["chat"].get("title") or "Saha Grubu"
                        set_saha_group(str(from_chat_id), g_title)
                        send_telegram_message(
                            f"✅ <b>Bu grup başarıyla 'Saha Grubu' olarak ayarlandı!</b>\n\n"
                            f"Grup: <b>{html.escape(g_title)}</b>\n"
                            f"ID: <code>{from_chat_id}</code>\n\n"
                            f"Düşen krediler otomatik olarak bu gruba etiketle iletilecektir.",
                            chat_id=from_chat_id
                        )

                    # /sahaci veya /sahaciyim Komutu
                    elif text.startswith("/sahaci") or text.startswith("/sahaciyim"):
                        if from_user.get("username"):
                            u_tag = f"@{from_user.get('username')}"
                            add_sahaci_user(u_tag)
                            send_telegram_message(
                                f"✅ {u_tag}, başarıyla <b>Sahacı Rolü</b>ne eklendiniz!\n\n"
                                f"Kredi düştüğünde bildirimlerde otomatik etiketleneceksiniz.",
                                chat_id=from_chat_id
                            )
                        else:
                            send_telegram_message(
                                "⚠️ Sahacı rolü alabilmek için lütfen bir Telegram Kullanıcı Adı (Username) belirleyin!",
                                chat_id=from_chat_id
                            )

                    # /grup_ekle Komutu
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
                        settings = get_settings()

                        lines = [f"📊 <b>Bot & Operasyon Durumu:</b>"]
                        lines.append(f"\n📋 <b>Kayıtlı Sheetler:</b> {len(sheets)}")
                        for idx, s in enumerate(sheets, 1):
                            emoji = "🟢" if s.get("active", True) else "⚪"
                            lines.append(f"{idx}. {emoji} <b>{html.escape(s['name'])}</b> ({s.get('count', 0)} kayıt)")

                        lines.append(f"\n🏢 <b>Saha Grubu:</b> {settings.get('saha_group_name', 'Belirlenmedi')} (<code>{settings.get('saha_group_id', 'Yok')}</code>)")
                        sahaci_list_str = ", ".join(settings.get("sahaci_users", [])) or "Yok"
                        lines.append(f"👷 <b>Sahacılar:</b> {sahaci_list_str}")

                        lines.append(f"\n👥 <b>Hedef Aktarım Grupları:</b> {len(groups)}")
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
    logger.info("  Google Sheets → Saha & Finans Etkileşim Botu Başlatıldı")
    sheets = get_sheets()
    logger.info(f"  Kayıtlı sheet sayısı: {len(sheets)}")
    logger.info(f"  Kontrol aralığı: {CHECK_INTERVAL} saniye ({CHECK_INTERVAL // 60} dakika)")
    logger.info("=" * 50)

    listener_thread = threading.Thread(target=listen_telegram_updates, daemon=True)
    listener_thread.start()

    try:
        check_all_sheets()
    except Exception as e:
        logger.error(f"İlk kontrolde hata: {e}")

    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            check_all_sheets()
        except Exception as e:
            logger.error(f"Kontrol sırasında hata: {e}")


if __name__ == "__main__":
    main()
