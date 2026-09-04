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
    reset_sheet_last_sent,
    record_message_sent,
    update_sheet_meta,
    extract_sheet_id,
    get_groups,
    add_group,
    get_settings,
    set_saha_group,
    get_main_chat_id,
    set_main_chat_id,
    add_sahaci_user,
    remove_sahaci_user,
    set_record_status,
    get_record_status,
    set_pending_action,
    get_pending_action,
    clear_pending_action,
    set_saha_rate,
    record_forward_event,
    get_forward_event,
    format_duration,
    get_original_message_id,
    get_next_global_id,
    get_record_global_id,
    normalize_phone,
    save_client_profile,
    check_client_history,
    log_activity_event,
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


def normalize_tr(text: str) -> str:
    """Türkçe karakterleri ve özel işaretleri temizler."""
    if not text:
        return ""
    tr_map = {'ı': 'i', 'İ': 'i', 'ş': 's', 'Ş': 's', 'ğ': 'g', 'Ğ': 'g', 'ü': 'u', 'Ü': 'u', 'ö': 'o', 'Ö': 'o', 'ç': 'c', 'Ç': 'c'}
    clean = text.strip().lower()
    for tr, en in tr_map.items():
        clean = clean.replace(tr, en)
    return clean.replace(" ", "_").replace(".", "").replace("?", "").replace("-", "_")


def find_column_mapping(headers: list[str]) -> dict[str, str]:
    mapping = {}
    for header in headers:
        norm = normalize_tr(header)
        # 1) Tarih
        if "created" in norm or "tarih" in norm or "zaman" in norm:
            mapping.setdefault("created_time", header)
        # 2) Çalışma Durumu
        elif "calisma" in norm or "meslek" in norm or "durum" in norm:
            mapping.setdefault("çalışma_durumu", header)
        # 3) TC Kimlik
        elif "tc" in norm or "kimlik" in norm:
            mapping.setdefault("t.c_numaranız", header)
        # 4) Kart Limiti
        elif "limit" in norm or "kart" in norm:
            mapping.setdefault("kullanılabilir_kart_limitiniz", header)
        # 5) Telefon Numarası
        elif "telefon" in norm or "phone" in norm or "tel" in norm or "gsm" in norm or "mobile" in norm:
            mapping.setdefault("phone_number", header)

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
    # Telefon numarasını temizle (varsa p: ön ekini kaldır) ve tam açık göster
    raw_phone = str(row.get(col_mapping.get("phone_number", ""), "") or "").strip()
    if raw_phone.startswith("p:"):
        raw_phone = raw_phone[2:]
    phone = html.escape(raw_phone) if raw_phone else "—"

    # MÜŞTERİ GEÇMİŞİ & MÜKERRER BAŞVURU KONTROLÜ
    history = check_client_history(phone, tc_no)
    warning_banner = ""
    if history:
        days_ago = history.get("days_ago", 0)
        last_st = history.get("last_status", "Belirtilmemiş")
        first_date = history.get("first_seen", "")
        last_note = history.get("last_note", "")

        note_part = f"\n📝 <b>Önceki Not:</b> <i>{html.escape(last_note)}</i>" if last_note else ""
        warning_banner = (
            f"⚠️ <b>DİKKAT: ESKİ / MÜKERRER BAŞVURU!</b>\n"
            f"📅 <b>İlk Başvuru:</b> {first_date} ({days_ago} gün önce)\n"
            f"📌 <b>Önceki Son Durum:</b> <b>{html.escape(last_st)}</b>{note_part}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        )

    # Yeni müşteri kaydını veritabanına işle
    save_client_profile(phone, tc_no, {
        "calisma_durumu": calisma_durumu,
        "kart_limit": kart_limit,
        "sheet": sheet_name,
        "entry_number": entry_number,
        "created_time": created_time
    }, status="Yeni Başvuru")

    message = (
        f"{warning_banner}"
        f"📋 <b>Kayıt #{entry_number}</b> — <i>{html.escape(sheet_name)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>Tarih:</b> {created_time}\n"
        f"💼 <b>Çalışma Durumu:</b> {calisma_durumu}\n"
        f"🆔 <b>T.C. Numarası:</b> <code>{tc_no}</code>\n"
        f"💳 <b>Kart Limiti:</b> {kart_limit}\n"
        f"📞 <b>Telefon:</b> <code>{phone}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    if status_note:
        message += f"\n{status_note}"

    return message


def build_record_keyboard(sheet_id: str, row_num: int, is_forwarded: bool = False) -> dict:
    """Kayıt altına yerleştirilecek standart etkileşim butonları."""
    buttons = [
        [
            {"text": "📝 Not Ekle", "callback_data": f"note_req:{sheet_id}:{row_num}"},
            {"text": "🔴 Olumsuz", "callback_data": f"st:{sheet_id}:{row_num}:olumsuz"}
        ],
        [
            {"text": "💳 Kredi Düştü", "callback_data": f"kredi_req:{sheet_id}:{row_num}"},
            {"text": "📵 Cevapsız", "callback_data": f"st:{sheet_id}:{row_num}:cevapsiz"}
        ]
    ]
    if is_forwarded:
        buttons.append([
            {"text": "🏁 İşi Bitir & Geri Çek", "callback_data": f"islem_bitti:{sheet_id}:{row_num}"}
        ])
    else:
        buttons.append([
            {"text": "↗️ Gruba Aktar", "callback_data": f"fwd_menu:{sheet_id}:{row_num}"}
        ])
    return {"inline_keyboard": buttons}


def send_telegram_message(text: str, chat_id: str = None, reply_markup: dict = None, reply_to_message_id: int = None) -> tuple[bool, int | None]:
    if not TELEGRAM_BOT_TOKEN:
        return False, None

    target_chat = str(chat_id or get_main_chat_id() or TELEGRAM_CHAT_ID or "-5529859923")
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
            elif reply_markup and data.get("error_code") == 400:
                # Eger buton/URL hatasi varsa butonsuz hemen tekrar dene
                payload.pop("reply_markup", None)
                res2 = requests.post(url, json=payload, timeout=15)
                d2 = res2.json()
                if d2.get("ok"):
                    return True, d2.get("result", {}).get("message_id")
                logger.error(f"Telegram API hatası (butonsuz deneme): {d2}")
                return False, None
            elif data.get("error_code") == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 15)
                time.sleep(retry_after + 1)
            else:
                logger.error(f"Telegram API hatası: {data}")
                return False, None
        except requests.RequestException as e:
            logger.error(f"Telegram gönderim hatası: {e}")
            time.sleep(2)

    return False, None


def edit_telegram_message(chat_id: int | str, message_id: int, text: str, reply_markup: dict = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return False
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
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            return True
        logger.error(f"Mesaj düzenleme hatası: {data}")
        # Eger HTML etiket/parse hatası varsa hemen düz metin fallback'i ile tekrar dene
        if data.get("error_code") == 400 and ("entities" in data.get("description", "").lower() or "tag" in data.get("description", "").lower() or "parse" in data.get("description", "").lower()):
            payload.pop("parse_mode", None)
            res2 = requests.post(url, json=payload, timeout=10)
            d2 = res2.json()
            if d2.get("ok"):
                return True
            logger.error(f"Mesaj düzenleme fallback hatası: {d2}")
        return False
    except Exception as e:
        logger.error(f"Mesaj düzenleme istisnası: {e}")
        return False


def delete_telegram_message(chat_id: int | str, message_id: int):
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=8)
    except Exception as e:
        logger.error(f"Mesaj silme hatası: {e}")


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
                            # KPI aktivitesine işle
                            log_activity_event(st_key, sheet_id, row_num, user_name=user_tag)

                            base_text = original_text.split("\n📌 Durum:")[0].split("\n📝 Not:")[0].split("\n↪️")[0].split("\n━━━━━━━━━━━━━━━━━━━━━━\n📌")[0].strip()
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

                    # 1.6.A) "❌ İşlem Kaçtı" Tıklandı
                    elif cq_data.startswith("islem_kacti:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])

                        set_record_status(sheet_id, row_num, "❌ İşlem Kaçtı", user_tag)
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")

                        # İlk gruba yanıt
                        answer_callback_query(cq_id, "İşlem kaçtı olarak kaydedildi.")
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + f"\n\n❌ <b>İşlem Kaçtı</b> ({html.escape(user_tag)} - {now_time})\n<i>🤝 Elinize sağlık!</i>",
                            reply_markup=None
                        )

                        # Saha Grubuna bildirim
                        settings = get_settings()
                        saha_chat_id = settings.get("saha_group_id")
                        if saha_chat_id:
                            send_telegram_message(
                                f"⚠️ <b>Kayıt #{row_num}</b> için işlem kaçtı, sonrakine buradayız! 🤝",
                                chat_id=saha_chat_id
                            )

                    # 1.6.B) "🎯 Atış Atıldı" Tıklandı -> Tutar İsteme Menüsü
                    elif cq_data.startswith("atis_req:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        default_amount = parts[3] if len(parts) > 3 else "10.000 TL"

                        atis_amount_kb = {
                            "inline_keyboard": [
                                [
                                    {"text": f"🎯 {default_amount}", "callback_data": f"atis_amt:{sheet_id}:{row_num}:{default_amount}"},
                                    {"text": "10.000 TL", "callback_data": f"atis_amt:{sheet_id}:{row_num}:10.000 TL"}
                                ],
                                [
                                    {"text": "20.000 TL", "callback_data": f"atis_amt:{sheet_id}:{row_num}:20.000 TL"},
                                    {"text": "50.000 TL", "callback_data": f"atis_amt:{sheet_id}:{row_num}:50.000 TL"}
                                ],
                                [
                                    {"text": "✍️ Özel Tutar Yaz", "callback_data": f"atis_custom:{sheet_id}:{row_num}"},
                                    {"text": "❌ İptal", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}
                                ]
                            ]
                        }
                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + "\n\n<i>🎯 Lütfen atılan tutarı seçiniz:</i>",
                            reply_markup=atis_amount_kb
                        )

                    # 1.6.C) Atış Tutarı Manuel Yazılacak
                    elif cq_data.startswith("atis_custom:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])

                        set_pending_action(user_id, {
                            "action": "atis_amount",
                            "sheet_id": sheet_id,
                            "row_num": row_num,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "original_text": original_text,
                        })
                        answer_callback_query(cq_id)
                        send_telegram_message(
                            f"✍️ {user_tag}, <b>Kayıt #{row_num}</b> için atılan net tutarı yazınız (Örn: <code>25.000 TL</code>):",
                            chat_id=chat_id,
                            reply_to_message_id=message_id
                        )

                    # 1.6.D) Atış Tutarı Seçildi -> SAHA GRUBUNA GÖNDER (Onay / Düşmedi Butonlarıyla)
                    elif cq_data.startswith("atis_amt:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        amt_str = parts[3]

                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        base_txt = original_text.split("\n\n<i>🎯")[0]

                        answer_callback_query(cq_id, f"Atış tutarı ({amt_str}) saha grubuna iletildi.")
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(base_txt) + f"\n\n🎯 <b>Atış Atıldı:</b> {html.escape(amt_str)} ({html.escape(user_tag)} - {now_time})\n<i>⏳ Saha onay bekleniyor...</i>",
                            reply_markup=None
                        )

                        # SAHA GRUBUNA ONAY / BLOKE / DÜŞMEDİ BUTONLARIYLA MESAJ
                        settings = get_settings()
                        saha_chat_id = settings.get("saha_group_id")
                        if saha_chat_id:
                            saha_confirm_kb = {
                                "inline_keyboard": [
                                    [
                                        {"text": "✅ Onay", "callback_data": f"saha_onay:{sheet_id}:{row_num}:{chat_id}:{message_id}:{amt_str}"},
                                        {"text": "🚫 Bloke", "callback_data": f"saha_bloke:{sheet_id}:{row_num}:{chat_id}:{message_id}:{amt_str}"},
                                        {"text": "⏳ Düşmedi", "callback_data": f"saha_dusmedi:{sheet_id}:{row_num}:{chat_id}:{message_id}:{amt_str}"}
                                    ]
                                ]
                            }
                            send_telegram_message(
                                f"🎯 <b>YENİ ATIŞ BİLGİSİ GELDİ!</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📋 <b>Kayıt:</b> #{row_num}\n"
                                f"💰 <b>Atılan Tutar:</b> <code>{amt_str}</code>\n"
                                f"👤 <b>Temsilci:</b> {html.escape(user_tag)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Hesabınızı kontrol edip lütfen durumu onaylayınız:",
                                chat_id=saha_chat_id,
                                reply_markup=saha_confirm_kb
                            )
                            # KPI için atış aktivitesi kaydet
                            amt_clean = float(re.sub(r"[^\d.]", "", amt_str.replace(".", "").replace(",", ".")) or 0)
                            log_activity_event("kredi", sheet_id, row_num, user_name=user_tag, amount=amt_clean)

                    # 1.6.E-1) Sahacı "🚫 Bloke" Dedi -> İLK GRUBA BİLGİ VER & PROFİLE BLOKE İŞLE
                    elif cq_data.startswith("saha_bloke:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        origin_chat_id = parts[3]
                        origin_msg_id = int(parts[4])
                        amt_str = parts[5]

                        set_record_status(sheet_id, row_num, "🚫 Bloke Oldu", user_tag)
                        log_activity_event("bloke", sheet_id, row_num, user_name=user_tag)

                        answer_callback_query(cq_id, "İşlem Bloke Oldu olarak kaydedildi ve bildirildi.", show_alert=True)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + f"\n\n🚫 <b>BLOKE OLDU</b> ({html.escape(user_tag)})",
                            reply_markup=None
                        )

                        # İlk gruba acil bloke uyarısı
                        send_telegram_message(
                            f"🚫 <b>DİKKAT: İŞLEM / HESAP BLOKE OLDU!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📋 <b>Kayıt:</b> #{row_num}\n"
                            f"💰 <b>Tutar:</b> {amt_str}\n"
                            f"👤 <b>İşaretleyen:</b> {html.escape(user_tag)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"⚠️ <i>Hesaba bloke konulduğu için işlem iptal edilmiştir. Müşteriye bilgi veriniz.</i>",
                            chat_id=origin_chat_id,
                            reply_to_message_id=origin_msg_id
                        )

                    # 1.6.E-2) Sahacı "⏳ Düşmedi" Dedi -> İLK GRUBA BİLGİ VER & SAHAYA "Şimdi Düştü" BUTONU BIRAK
                    elif cq_data.startswith("saha_dusmedi:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        origin_chat_id = parts[3]
                        origin_msg_id = int(parts[4])
                        amt_str = parts[5]

                        answer_callback_query(cq_id, "Düşmedi bildirimi ilk gruba iletildi.")
                        # Saha grubundaki mesajı güncelle ama "Şimdi Düştü" butonunu açık tut!
                        later_confirm_kb = {
                            "inline_keyboard": [
                                [
                                    {"text": "✅ Para Şimdi Düştü (Onayla)", "callback_data": f"saha_onay:{sheet_id}:{row_num}:{origin_chat_id}:{origin_msg_id}:{amt_str}"}
                                ]
                            ]
                        }
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + f"\n\n⏳ <b>Düşmedi Olarak İşaretlendi</b> ({html.escape(user_tag)})\n<i>(Hesaba düşerse aşağıdaki butonla onaylayabilirsiniz)</i>",
                            reply_markup=later_confirm_kb
                        )

                        # İLK GRUBA BİLGİ VER
                        send_telegram_message(
                            f"⏳ <b>Kayıt #{row_num}</b> için tutar ({amt_str}) henüz hesaba düşmedi, bekleniyor. Hesaba düştüğünde saha ekibi onaylayacaktır.",
                            chat_id=origin_chat_id,
                            reply_to_message_id=origin_msg_id
                        )

                    # 1.6.F) Sahacı "✅ Onay" Verdi -> HAKEDİŞ HESABI & İLK GRUBA ONAY BİLDİRİMİ
                    elif cq_data.startswith("saha_onay:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        origin_chat_id = parts[3]
                        origin_msg_id = int(parts[4])
                        amt_str = parts[5]

                        # Tutar parse (sayısal)
                        num_matches = re.findall(r"\d+", amt_str.replace(".", "").replace(",", ""))
                        raw_amount = float(num_matches[0]) if num_matches else 0.0

                        settings = get_settings()
                        rate = float(settings.get("saha_rate", 15.0))
                        saha_hakedis = (raw_amount * rate) / 100.0
                        net_kalan = raw_amount - saha_hakedis

                        log_activity_event("onay", sheet_id, row_num, user_name=user_tag, amount=raw_amount)

                        answer_callback_query(cq_id, "İşlem başarıyla onaylandı! ✅", show_alert=True)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + f"\n\n✅ <b>Onaylandı</b> ({html.escape(user_tag)})\n💰 Hakediş (%{rate:g}): {saha_hakedis:,.0f} TL",
                            reply_markup=None
                        )

                        # İLK GRUBA SADECE ONAY VE NET TUTAR MESAJI (Hakediş gizli!)
                        onay_msg = (
                            f"✅ <b>KREDİ / ATIŞ İŞLEMİ ONAYLANDI!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📋 <b>Kayıt:</b> #{row_num}\n"
                            f"💰 <b>Onaylanan Tutar:</b> <code>{amt_str}</code>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👉 <i>Lütfen sonraki adımı seçiniz:</i>"
                        )
                        next_step_kb = {
                            "inline_keyboard": [
                                [
                                    {"text": "▶️ Devam Et", "callback_data": f"devam_et:{sheet_id}:{row_num}"},
                                    {"text": "🏁 İşlem Bitti", "callback_data": f"islem_bitti:{sheet_id}:{row_num}:{amt_str}"}
                                ]
                            ]
                        }
                        send_telegram_message(onay_msg, chat_id=origin_chat_id, reply_markup=next_step_kb, reply_to_message_id=origin_msg_id)

                    # 1.6.G) "▶️ Devam Et" Tıklandı
                    elif cq_data.startswith("devam_et:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        answer_callback_query(cq_id, "Kayıt açık tutuluyor, devam edebilirsiniz.")
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(original_text) + f"\n\n▶️ <i>İşleme devam ediliyor... ({html.escape(user_tag)})</i>",
                            reply_markup=build_record_keyboard(sheet_id, row_num)
                        )

                    # 1.6.H) "🏁 İşlem Bitti" Tıklandı -> DATAYI GERİ ÇEK VE SÜRE ANALİZİ RAPORLA
                    elif cq_data.startswith("islem_bitti:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        amt_str = parts[3] if len(parts) > 3 else ""

                        status_text = f"🏁 İşlem Bitti ({amt_str})" if amt_str else "🏁 İşlem Bitti"
                        set_record_status(sheet_id, row_num, status_text, user_tag)
                        answer_callback_query(cq_id, "İşlem tamamlandı, veri ana gruba geri çekiliyor... 🏁", show_alert=True)

                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M:%S")
                        now_ts = time.time()

                        # Aktarım bilgilerini kontrol et
                        fwd_info = get_forward_event(sheet_id, row_num)
                        duration_str = "Bilinmiyor"
                        target_g_name = "Aktarılan Grup"
                        fwd_user = "Bilinmiyor"
                        fwd_time_str = "Bilinmiyor"

                        if fwd_info:
                            target_chat_id = fwd_info.get("target_chat_id")
                            target_msg_id = fwd_info.get("target_msg_id")
                            target_g_name = fwd_info.get("target_chat_name", "Aktarılan Grup")
                            fwd_user = fwd_info.get("fwd_user", "Temsilci")
                            fwd_time_str = fwd_info.get("fwd_time_str", "")
                            fwd_ts = fwd_info.get("fwd_timestamp", now_ts)
                            diff_sec = max(0, now_ts - fwd_ts)
                            duration_str = format_duration(diff_sec)

                            # 1) AKTARILAN HEDEF GRUPTAKİ MESAJI SİL / GERİ ÇEK
                            if target_chat_id and target_msg_id:
                                delete_telegram_message(target_chat_id, target_msg_id)
                                logger.info(f"Kayıt #{row_num} aktarılan gruptan ({target_g_name}) geri çekildi.")

                        # Eğer buton işlem grubunda tıklandıysa ve target_chat_id ile bu chat aynıysa bu mesajı sil
                        if str(chat_id) != str(TELEGRAM_CHAT_ID):
                            delete_telegram_message(chat_id, message_id)
                        else:
                            # Ana gruptaysa durumunu güncelle
                            clean_base_main = original_text.split("\n\n🏁")[0].split("\n\n<i>")[0]
                            edit_telegram_message(
                                chat_id, message_id,
                                html.escape(clean_base_main) + f"\n\n🏁 <b>İşlem Başarıyla Tamamlandı</b> ({html.escape(user_tag)} - {now_time})\n<i>🔄 Veri ana gruba geri çekildi.</i>",
                                reply_markup=None
                            )

                        # 3) ANA DATA GRUBUNA GERİ ÇEKME & DETAYLI SÜRE ANALİZ RAPORU GÖNDER
                        main_chat_id = TELEGRAM_CHAT_ID
                        if main_chat_id:
                            clean_base = original_text.split("\n\n🏁")[0].split("\n\n<i>")[0].split("\n\n❓")[0]
                            analiz_raporu = (
                                f"📥 <b>DATA GERİ ÇEKİLDİ & İŞLEM RAPORU</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📋 <b>Kayıt:</b> #{row_num}\n"
                                f"🏢 <b>İşlem Grubu:</b> <b>{html.escape(target_g_name)}</b>\n"
                                f"⏱️ <b>Grupta Kalma Süresi:</b> <b>{duration_str}</b>\n"
                                f"👤 <b>İşlemi Yapan:</b> {html.escape(user_tag)}\n"
                                f"📌 <b>Nihai Yanıt / Durum:</b> <b>{html.escape(status_text)}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"{html.escape(clean_base)}"
                            )
                            # Orijinal mesaj varsa ona yanıt olarak gönder, yoksa doğrudan ana gruba at
                            orig_msg_id = get_original_message_id(sheet_id, row_num)
                            send_telegram_message(analiz_raporu, chat_id=main_chat_id, reply_to_message_id=orig_msg_id)

                    # 1.7) Gruba Aktar Menüsü
                    elif cq_data.startswith("fwd_menu:"):
                        parts = cq_data.split(":")
                        sheet_id = parts[1]
                        row_num = int(parts[2])
                        groups = get_groups()

                        if not groups:
                            answer_callback_query(
                                cq_id,
                                "⚠️ Henüz aktarılacak hedef grup eklenmemiş!\nLütfen önce aktarmak istediğiniz grupta /grup_ekle [Grup Adı] yazın veya panelden grup ekleyin.",
                                show_alert=True
                            )
                        else:
                            group_buttons = []
                            for g in groups:
                                group_buttons.append([{"text": f"👥 {g['name']}", "callback_data": f"fwd_sel:{sheet_id}:{row_num}:{g['id']}"}])

                            group_buttons.append([{"text": "❌ İptal", "callback_data": f"fwd_cancel:{sheet_id}:{row_num}"}])

                            clean_orig = original_text.split("\n\n<i>↪️")[0]
                            answer_callback_query(cq_id)
                            edit_telegram_message(
                                chat_id, message_id,
                                html.escape(clean_orig) + "\n\n<i>↪️ Hangi gruba aktarmak istiyorsunuz?</i>",
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
                        clean_orig = original_text.replace("\n\n<i>↪️ Hangi gruba aktarmak istiyorsunuz?</i>", "").split("\n\n❓")[0]
                        answer_callback_query(cq_id)
                        edit_telegram_message(
                            chat_id, message_id,
                            html.escape(clean_orig) +
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

                        success, target_msg_id = send_telegram_message(
                            html.escape(clean_lead_text) + f"\n\n<i>(Aktaran: {html.escape(user_tag)})</i>",
                            chat_id=target_chat_id,
                            reply_markup=build_record_keyboard(sheet_id, row_num, is_forwarded=True)
                        )

                        # Aktarım olayını, hedef grup ve mesaj ID'sini kaydet (Geri çekme ve süre analizi için)
                        if target_msg_id:
                            record_forward_event(sheet_id, row_num, target_chat_id, target_name, target_msg_id, user_tag)

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
                elif any(k in update for k in ["message", "channel_post", "edited_message"]):
                    msg = update.get("message") or update.get("channel_post") or update.get("edited_message")
                    if not msg or not isinstance(msg, dict):
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    from_user = msg.get("from") or {}
                    user_id = from_user.get("id") or msg.get("chat", {}).get("id")
                    user_tag = f"@{from_user.get('username')}" if from_user.get("username") else (from_user.get("first_name") or "Temsilci")
                    from_chat_id = msg["chat"]["id"]

                    # Komutlardaki bot username etiketini temizle (Örn: /help@palamutarmutbot -> /help)
                    if text.startswith("/"):
                        first_word = text.split()[0]
                        if "@" in first_word:
                            clean_cmd = first_word.split("@")[0]
                            text = clean_cmd + text[len(first_word):]

                    is_command = text.startswith("/")

                    if is_command and text.lower().strip() in ["/iptal", "/cancel"]:
                        clear_pending_action(user_id)
                        send_telegram_message("❌ Bekleyen işlem iptal edildi.", chat_id=from_chat_id)
                        continue

                    # 2.A) BEKLEYEN YANITLAR (Yalnızca komut DEĞİLSE çalışır!)
                    pending = get_pending_action(user_id) if not is_command else None

                    # Eğer bu kişi bir not yazıyorsa
                    if pending and pending.get("action") == "note":
                        sheet_id = pending["sheet_id"]
                        row_num = pending["row_num"]
                        target_chat_id = pending["chat_id"]
                        target_msg_id = pending["message_id"]
                        orig_text = pending["original_text"]

                        note_str = f"📝 Not: {text}"
                        set_record_status(sheet_id, row_num, note_str, user_tag, note=text)
                        log_activity_event("note", sheet_id, row_num, user_name=user_tag, extra=text)

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

                        # İLK ORİJİNAL GRUBA MESAJ AT (Altında Atış Atıldı / İşlem Kaçtı butonları ile)
                        orig_notify = (
                            f"💳 <b>KREDİ ONAYI İÇİN IBAN BİLGİSİ GELDİ!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📋 <b>Kayıt:</b> #{row_num}\n"
                            f"💰 <b>Tutar:</b> {amount_str}\n"
                            f"👤 <b>Sahacı:</b> {html.escape(user_tag)}\n"
                            f"🏦 <b>IBAN / Bilgiler:</b>\n"
                            f"<code>{html.escape(iban_info)}</code>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👉 <i>Lütfen aşağıdaki butonlarla işlemin sonucunu seçin:</i>"
                        )
                        atis_keyboard = {
                            "inline_keyboard": [
                                [
                                    {"text": "🎯 Atış Atıldı", "callback_data": f"atis_req:{sheet_id}:{row_num}:{amount_str}"},
                                    {"text": "❌ İşlem Kaçtı", "callback_data": f"islem_kacti:{sheet_id}:{row_num}"}
                                ]
                            ]
                        }
                        send_telegram_message(orig_notify, chat_id=origin_chat_id, reply_markup=atis_keyboard, reply_to_message_id=origin_msg_id)
                        continue

                    # Eğer atış tutarını manuel yazıyorsa -> SAHA GRUBUNA İLET
                    elif pending and pending.get("action") == "atis_amount":
                        sheet_id = pending["sheet_id"]
                        row_num = pending["row_num"]
                        target_chat_id = pending["chat_id"]
                        target_msg_id = pending["message_id"]
                        orig_text = pending["original_text"]
                        amt_str = text if "TL" in text.upper() else f"{text} TL"

                        clear_pending_action(user_id)
                        now_time = datetime.now(TURKEY_TZ).strftime("%H:%M")
                        base_txt = orig_text.split("\n\n<i>🎯")[0]

                        send_telegram_message(f"✅ Atış tutarı ({amt_str}) saha grubuna iletildi.", chat_id=from_chat_id)
                        edit_telegram_message(
                            target_chat_id, target_msg_id,
                            html.escape(base_txt) + f"\n\n🎯 <b>Atış Atıldı:</b> {amt_str} ({html.escape(user_tag)} - {now_time})\n<i>⏳ Saha onay bekleniyor...</i>",
                            reply_markup=None
                        )

                        # Saha Grubuna Onay / Düşmedi butonlarıyla yolla
                        settings = get_settings()
                        saha_chat_id = settings.get("saha_group_id")
                        if saha_chat_id:
                            saha_confirm_kb = {
                                "inline_keyboard": [
                                    [
                                        {"text": "✅ Onay", "callback_data": f"saha_onay:{sheet_id}:{row_num}:{target_chat_id}:{target_msg_id}:{amt_str}"},
                                        {"text": "⏳ Düşmedi", "callback_data": f"saha_dusmedi:{sheet_id}:{row_num}:{target_chat_id}:{target_msg_id}:{amt_str}"}
                                    ]
                                ]
                            }
                            send_telegram_message(
                                f"🎯 <b>YENİ ATIŞ BİLGİSİ GELDİ!</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📋 <b>Kayıt:</b> #{row_num}\n"
                                f"💰 <b>Atılan Tutar:</b> <code>{amt_str}</code>\n"
                                f"👤 <b>Temsilci:</b> {html.escape(user_tag)}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Hesabınızı kontrol edip lütfen durumu onaylayınız:",
                                chat_id=saha_chat_id,
                                reply_markup=saha_confirm_kb
                            )
                        continue

                    parts = text.split()
                    cmd = text.lower()
                    base_cmd = parts[0].split("@")[0].lower() if parts else ""

                    # 2.B) STANDART KOMUTLAR

                    # /sahaorani Komutu (Örn: /sahaorani 15 veya /sahaorani 20)
                    if base_cmd == "/sahaorani" or cmd.startswith("/sahaorani"):
                        if len(parts) > 1:
                            try:
                                clean_rate = float(parts[1].replace("%", "").replace(",", "."))
                                set_saha_rate(clean_rate)
                                send_telegram_message(
                                    f"✅ <b>Saha Oranı Güncellendi!</b>\n\nYeni Hakediş Oranı: <b>%{clean_rate:g}</b>",
                                    chat_id=from_chat_id
                                )
                            except ValueError:
                                send_telegram_message("⚠️ Lütfen geçerli bir sayı girin! Örn: <code>/sahaorani 15</code>", chat_id=from_chat_id)
                        else:
                            curr_rate = get_settings().get("saha_rate", 15.0)
                            send_telegram_message(
                                f"📊 <b>Mevcut Saha Oranı:</b> %{curr_rate:g}\n\nDeğiştirmek için: <code>/sahaorani 20</code>",
                                chat_id=from_chat_id
                            )
                        continue

                    # /help veya /yardim Komutu
                    elif base_cmd in ["/help", "/yardim", "/komutlar"] or cmd.startswith(("/help", "/yardim")):
                        help_text = (
                            "📖 <b>BOT KULLANIM KILAVUZU & TÜM KOMUTLAR</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            "🏢 <b>Saha & Finans Operasyon Komutları:</b>\n"
                            "• <code>/saha_grubu</code>\n"
                            "  ↳ <i>Bulunduğunuz grubu 'Saha Grubu' olarak tanımlar. Kredi düşen datalar otomatik buraya yönlendirilir.</i>\n\n"
                            "• <code>/sahaci @kullaniciadi</code> veya <code>/sahaci</code>\n"
                            "  ↳ <i>Belirtilen kullanıcıyı (veya kendinizi) sahacı rolüne ekler. Kredi düşüşlerinde otomatik etiketlenirler.</i>\n\n"
                            "• <code>/sahaci_sil @kullaniciadi</code> veya <code>/sahaci sil @kullaniciadi</code>\n"
                            "  ↳ <i>Belirtilen kullanıcıyı sahacı listesinden çıkarır.</i>\n\n"
                            "• <code>/sahacilar</code>\n"
                            "  ↳ <i>Mevcut kayıtlı sahacıların listesini gösterir.</i>\n\n"
                            "• <code>/sahaorani [oran]</code>\n"
                            "  ↳ <i>Saha hakediş yüzdesini günceller. Örn: <code>/sahaorani 15</code> veya <code>/sahaorani 20</code>.</i>\n\n"
                            "👥 <b>Grup & Data Yönetim Komutları:</b>\n"
                            "• <code>/grup_ekle [Grup Adı]</code>\n"
                            "  ↳ <i>Bulunulan grubu 'Aktarılacak Hedef Gruplar' listesine ekler. Örn: <code>/grup_ekle Satış Ekibi 1</code>.</i>\n\n"
                            "• <code>/link [Google Sheets Linki]</code>\n"
                            "  ↳ <i>Yeni bir Google E-Tablo linkini bota ekler. Bot yeni satırları otomatik çeker.</i>\n\n"
                            "📊 <b>Genel & Durum Komutları:</b>\n"
                            "• <code>/aktar</code> veya <code>/gonder</code>\n"
                            "  ↳ <i>Kayıtlı e-tablolardaki verileri sıfırlayıp gruba etkileşim butonlarıyla aktarır.</i>\n\n"
                            "• <code>/durum</code>\n"
                            "  ↳ <i>Aktif sheetleri, saha grubunu, kayıtlı sahacıları ve hedef grupları listeler.</i>\n\n"
                            "• <code>/panel</code>\n"
                            "  ↳ <i>Telegram içi Mini App panelini açar (Dış web sitesine yönlendirmez).</i>\n\n"
                            "• <code>/start</code>\n"
                            "  ↳ <i>Botu başlatır ve temel menüyü gösterir.</i>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            "💡 <b>İşlem Butonları Akışı:</b>\n"
                            "1. <b>Not Ekle:</b> Kayda müşteriyle ilgili not ekler.\n"
                            "2. <b>Kredi Düştü:</b> Tutar seçilir → Saha grubuna bildirim gider.\n"
                            "3. <b>IBAN Paylaş:</b> Sahacı IBAN yollar → Temsilci grubuna düşer.\n"
                            "4. <b>Atış Atıldı / İşlem Kaçtı:</b> Tutar onaylanır veya kapatılır.\n"
                            "5. <b>İşlem Bitti:</b> Data aktarılan gruptan silinir, ana gruba <b>Süre Analizi</b> ile geri çekilir!"
                        )
                        markup = None
                        if PUBLIC_URL:
                            markup = {"inline_keyboard": [[{"text": "📊 Mini App Paneli Aç", "web_app": {"url": PUBLIC_URL}}]]}
                        send_telegram_message(help_text, chat_id=from_chat_id, reply_markup=markup)
                        continue

                    # /start Komutu
                    elif base_cmd == "/start" or cmd.startswith("/start"):
                        welcome_text = (
                            "👋 <b>Google Sheets Saha & Finans Botu Aktif!</b>\n\n"
                            "Komutlar:\n"
                            "• <code>/aktar</code> - Tablodaki verileri gruba aktarır\n"
                            "• <code>/link &lt;Link&gt;</code> - Yeni Google Sheets ekler/günceller\n"
                            "• <code>/saha_grubu</code> - Bulunulan grubu Saha Grubu yapar\n"
                            "• <code>/sahaci @kullaniciadi</code> - Sahacı listesine kullanıcı ekler\n"
                            "• <code>/sahaci_sil @kullaniciadi</code> - Sahacı listesinden çıkarır\n"
                            "• <code>/sahacilar</code> - Kayıtlı sahacıları listeler\n"
                            "• <code>/grup_ekle &lt;Grup Adı&gt;</code> - Grubu aktarım listesine ekler\n"
                            "• <code>/durum</code> - Tüm grupları ve ayarları gösterir\n"
                            "• <code>/panel</code> - Telegram içi Mini App panelini açar"
                        )
                        markup = None
                        if PUBLIC_URL:
                            markup = {"inline_keyboard": [[{"text": "📊 Mini App Paneli Aç", "web_app": {"url": PUBLIC_URL}}]]}
                        send_telegram_message(welcome_text, chat_id=from_chat_id, reply_markup=markup)

                    # /data_grubu veya /ana_grup Komutu (Ana gelen kutusu grubunu ayarlar)
                    elif base_cmd in ["/data_grubu", "/ana_grup", "/ana_grup_yap", "/datagrubu"]:
                        g_title = msg["chat"].get("title") or "Ana Data Grubu"
                        set_main_chat_id(str(from_chat_id), g_title)
                        send_telegram_message(
                            f"✅ <b>Bu grup 'Ana Data Grubu' olarak ayarlandı!</b>\n\n"
                            f"Grup: <b>{html.escape(g_title)}</b>\n"
                            f"ID: <code>{from_chat_id}</code>\n\n"
                            f"Eklenen Google E-Tablo linklerindeki tüm kayıtlar artık bu gruba gönderilecektir.",
                            chat_id=from_chat_id
                        )

                    # /saha_grubu Komutu
                    elif base_cmd == "/saha_grubu" or cmd.startswith("/saha_grubu"):
                        g_title = msg["chat"].get("title") or "Saha Grubu"
                        set_saha_group(str(from_chat_id), g_title)
                        send_telegram_message(
                            f"✅ <b>Bu grup başarıyla 'Saha Grubu' olarak ayarlandı!</b>\n\n"
                            f"Grup: <b>{html.escape(g_title)}</b>\n"
                            f"ID: <code>{from_chat_id}</code>\n\n"
                            f"Düşen krediler otomatik olarak bu gruba etiketle iletilecektir.",
                            chat_id=from_chat_id
                        )

                    # /sahaci veya /sahaciyim veya /sahaci_ekle Komutu
                    elif base_cmd in ["/sahaci", "/sahaciyim", "/sahaci_ekle"]:
                        # 1. /sahaci sil @user kontrolü
                        if len(parts) > 1 and parts[1].lower() in ["sil", "çıkar", "cikar", "delete", "remove", "del"]:
                            del_targets = parts[2:]
                            if not del_targets and msg.get("reply_to_message"):
                                rep_uname = msg.get("reply_to_message", {}).get("from", {}).get("username")
                                if rep_uname:
                                    del_targets = [f"@{rep_uname}"]

                            if del_targets:
                                removed = []
                                for t in del_targets:
                                    if remove_sahaci_user(t):
                                        t_tag = t if t.startswith("@") else f"@{t}"
                                        removed.append(t_tag)
                                all_s = get_settings().get("sahaci_users", [])
                                all_s_str = ", ".join(all_s) if all_s else "Yok"
                                if removed:
                                    send_telegram_message(
                                        f"🗑️ <b>Sahacı Rolü Kaldırıldı!</b>\n\n"
                                        f"Çıkarılan: <b>{', '.join(removed)}</b>\n"
                                        f"📋 <b>Kalan Sahacılar:</b> {all_s_str}",
                                        chat_id=from_chat_id
                                    )
                                else:
                                    send_telegram_message(
                                        f"ℹ️ Belirtilen kullanıcı(lar) zaten sahacı listesinde yok.\n📋 <b>Mevcut Sahacılar:</b> {all_s_str}",
                                        chat_id=from_chat_id
                                    )
                            else:
                                send_telegram_message(
                                    "⚠️ Kimi çıkarmak istiyorsunuz? Örn: <code>/sahaci sil @kullaniciadi</code>",
                                    chat_id=from_chat_id
                                )
                            continue

                        # 2. Parametre olarak username(ler) verilmiş mi? (Örn: /sahaci @ahmet veya /sahaci ahmet)
                        targets_to_add = parts[1:]
                        if not targets_to_add and msg.get("reply_to_message"):
                            # Mesaj yanıtlanarak /sahaci yazılmış
                            rep_uname = msg.get("reply_to_message", {}).get("from", {}).get("username")
                            if rep_uname:
                                targets_to_add = [f"@{rep_uname}"]
                            else:
                                send_telegram_message(
                                    "⚠️ Yanıtladığınız kullanıcının Telegram Kullanıcı Adı (Username) bulunamadı. Lütfen kullanıcı adını yazarak ekleyin:\n👉 <code>/sahaci @kullaniciadi</code>",
                                    chat_id=from_chat_id
                                )
                                continue

                        if targets_to_add:
                            added_users = []
                            for target in targets_to_add:
                                added_tag = add_sahaci_user(target)
                                if added_tag:
                                    added_users.append(added_tag)

                            all_s = get_settings().get("sahaci_users", [])
                            all_s_str = ", ".join(all_s) if all_s else "Yok"

                            if added_users:
                                send_telegram_message(
                                    f"✅ <b>Sahacı Rolü Tanımlandı!</b>\n\n"
                                    f"👷 <b>Eklenen:</b> {', '.join(added_users)}\n"
                                    f"📋 <b>Tüm Sahacılar:</b> {all_s_str}\n\n"
                                    f"Kredi düştüğünde bildirimlerde otomatik etiketlenecekler.",
                                    chat_id=from_chat_id
                                )
                            else:
                                send_telegram_message(
                                    "⚠️ Geçerli bir kullanıcı adı tespit edilemedi. Örn: <code>/sahaci @kullaniciadi</code>",
                                    chat_id=from_chat_id
                                )
                        else:
                            # Parametre verilmemiş ve yanıt değil -> Komutu yazan kişi kendini ekliyor
                            if from_user.get("username"):
                                u_tag = f"@{from_user.get('username')}"
                                add_sahaci_user(u_tag)
                                all_s = get_settings().get("sahaci_users", [])
                                all_s_str = ", ".join(all_s) if all_s else "Yok"
                                send_telegram_message(
                                    f"✅ <b>{u_tag}</b>, başarıyla <b>Sahacı Rolü</b>ne eklendiniz!\n\n"
                                    f"📋 <b>Tüm Sahacılar:</b> {all_s_str}\n\n"
                                    f"Kredi düştüğünde bildirimlerde otomatik etiketleneceksiniz.",
                                    chat_id=from_chat_id
                                )
                            else:
                                all_s = get_settings().get("sahaci_users", [])
                                all_s_str = ", ".join(all_s) if all_s else "Yok"
                                send_telegram_message(
                                    "⚠️ <b>Sahacı Ekleme Formatı:</b>\n\n"
                                    "• Başka birini eklemek için: <code>/sahaci @kullaniciadi</code>\n"
                                    "• Mesajını yanıtlayıp: <code>/sahaci</code>\n"
                                    "• Kendinizi eklemek için: Telegram profilinizden bir Kullanıcı Adı (Username) belirleyin.\n\n"
                                    f"📋 <b>Mevcut Sahacılar:</b> {all_s_str}",
                                    chat_id=from_chat_id
                                )

                    # /sahaci_sil veya /sahacisil Komutu
                    elif base_cmd in ["/sahaci_sil", "/sahacisil", "/sahaci_cikar", "/sahacicikar"]:
                        targets = parts[1:]
                        if not targets and msg.get("reply_to_message"):
                            rep_uname = msg.get("reply_to_message", {}).get("from", {}).get("username")
                            if rep_uname:
                                targets = [f"@{rep_uname}"]

                        if targets:
                            removed = []
                            for t in targets:
                                if remove_sahaci_user(t):
                                    removed.append(t if t.startswith("@") else f"@{t}")
                            all_s = get_settings().get("sahaci_users", [])
                            all_s_str = ", ".join(all_s) if all_s else "Yok"
                            if removed:
                                send_telegram_message(
                                    f"🗑️ <b>Sahacı Rolü Kaldırıldı!</b>\n\n"
                                    f"Çıkarılan: <b>{', '.join(removed)}</b>\n"
                                    f"📋 <b>Kalan Sahacılar:</b> {all_s_str}",
                                    chat_id=from_chat_id
                                )
                            else:
                                send_telegram_message(
                                    f"ℹ️ Belirtilen kullanıcı(lar) zaten sahacı listesinde bulunamadı.\n📋 <b>Mevcut Sahacılar:</b> {all_s_str}",
                                    chat_id=from_chat_id
                                )
                        else:
                            send_telegram_message(
                                "⚠️ Çıkarmak istediğiniz kişiyi belirtin:\nÖrn: <code>/sahaci_sil @kullaniciadi</code>",
                                chat_id=from_chat_id
                            )

                    # /sahacilar veya /sahaci_liste Komutu
                    elif base_cmd in ["/sahacilar", "/sahaci_liste", "/sahaciler"]:
                        all_s = get_settings().get("sahaci_users", [])
                        all_s_str = "\n".join([f"• <b>{u}</b>" for u in all_s]) if all_s else "<i>Henüz kayıtlı sahacı yok.</i>"
                        send_telegram_message(
                            f"👷 <b>Kayıtlı Sahacılar Listesi:</b>\n\n{all_s_str}\n\n"
                            f"➕ <b>Ekle:</b> <code>/sahaci @kullaniciadi</code>\n"
                            f"➖ <b>Çıkar:</b> <code>/sahaci_sil @kullaniciadi</code>",
                            chat_id=from_chat_id
                        )

                    # /grup_ekle Komutu
                    elif cmd.startswith("/grup_ekle"):
                        parts = text.split(maxsplit=1)
                        g_name = parts[1].strip() if len(parts) > 1 else (msg["chat"].get("title") or "Yeni Grup")
                        success, resp_msg = add_group(g_name, str(from_chat_id))
                        if success:
                            reply = f"✅ <b>Grup Başarıyla Eklendi!</b>\n\nİsim: <b>{html.escape(g_name)}</b>\nID: <code>{from_chat_id}</code>\n\nArtık kayıtları bu gruba aktarabilirsiniz."
                        else:
                            reply = f"ℹ️ {resp_msg}"
                        send_telegram_message(reply, chat_id=from_chat_id)

                    # /link Komutu (Yeni Google Sheets Ekleme veya Güncelleme)
                    elif base_cmd == "/link" or cmd.startswith("/link") or "docs.google.com/spreadsheets" in text:
                        parts = text.split(maxsplit=1)
                        target_url = parts[1].strip() if len(parts) > 1 else text.strip()

                        match = re.search(r"https?://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9-_]+[^\s]*", target_url)
                        if match:
                            clean_url = match.group(0)
                            target_group = str(from_chat_id) if str(from_chat_id).startswith("-") else get_main_chat_id()
                            success, msg_resp = add_sheet("", clean_url, chat_id=target_group)

                            if success:
                                s_id = extract_sheet_id(clean_url)
                                added_sheet = next((s for s in get_sheets() if s.get("id") == s_id), None)
                                sheet_name = added_sheet.get("name") if added_sheet else "E-Tablo"

                                reply_msg = (
                                    f"✅ <b>Google Sheets Başarıyla Eklendi!</b>\n\n"
                                    f"📝 <b>Tablo Adı:</b> <b>{html.escape(sheet_name)}</b>\n"
                                    f"🎯 <b>Hedef Grup:</b> <code>{target_group}</code>\n"
                                    f"🔗 <b>Link:</b> {clean_url}\n\n"
                                    f"🚀 Kayıtlar <code>{target_group}</code> grubuna etkileşim butonlarıyla aktarılıyor..."
                                )
                                send_telegram_message(reply_msg, chat_id=from_chat_id)

                                # Hemen sheet'i tara ve tüm kayıtları zorunlu olarak gruba aktar!
                                try:
                                    threading.Thread(
                                        target=check_and_send_sheet,
                                        args=({"name": sheet_name, "url": clean_url, "id": s_id, "chat_id": target_group}, True),
                                        daemon=True
                                    ).start()
                                except Exception as e:
                                    logger.error(f"check_and_send_sheet thread error: {e}")
                            else:
                                send_telegram_message(f"ℹ️ {msg_resp}", chat_id=from_chat_id)
                        else:
                            send_telegram_message("⚠️ <b>Geçerli bir link bulunamadı!</b>\nKullanım: <code>/link https://docs.google.com/...</code>", chat_id=from_chat_id)

                    # /aktar veya /gonder Komutu (Kayıtları Gruba Manuel Aktar)
                    elif base_cmd in ["/aktar", "/gonder", "/yenile", "/sync"]:
                        sheets = get_sheets()
                        active_sheets = [s for s in sheets if s.get("active", True)]
                        if not active_sheets:
                            send_telegram_message("⚠️ Aktarılacak kayıtlı bir e-tablo bulunamadı. Lütfen <code>/link [Google Sheets Linki]</code> ile tablo ekleyin.", chat_id=from_chat_id)
                        else:
                            target_group = str(from_chat_id) if str(from_chat_id).startswith("-") else get_main_chat_id()
                            send_telegram_message(f"🚀 <b>Aktarım Başlatıldı!</b>\n\nKayıtlı e-tablolardaki tüm veriler <code>{target_group}</code> grubuna etkileşim butonlarıyla aktarılıyor...", chat_id=from_chat_id)
                            for s in active_sheets:
                                s_cfg = dict(s)
                                s_cfg["chat_id"] = target_group
                                reset_sheet_last_sent(s["id"])
                                threading.Thread(
                                    target=check_and_send_sheet,
                                    args=(s_cfg, True),
                                    daemon=True
                                ).start()

                    # /sifirla Komutu (Aktarım sayaçlarını sıfırla)
                    elif base_cmd in ["/sifirla", "/reset"]:
                        reset_sheet_last_sent()
                        target_group = str(from_chat_id) if str(from_chat_id).startswith("-") else get_main_chat_id()
                        send_telegram_message(f"🔄 <b>Aktarım sayaçları sıfırlandı!</b>\nTüm kayıtlar <code>{target_group}</code> grubuna baştan aktarılıyor...", chat_id=from_chat_id)
                        for s in get_sheets():
                            if s.get("active", True):
                                s_cfg = dict(s)
                                s_cfg["chat_id"] = target_group
                                threading.Thread(
                                    target=check_and_send_sheet,
                                    args=(s_cfg, True),
                                    daemon=True
                                ).start()

                    # /durum Komutu
                    elif cmd.startswith("/durum"):
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

                    # /panel Komutu (Telegram Mini App - Dış browser yok)
                    elif cmd.startswith("/panel"):
                        if PUBLIC_URL:
                            markup = {"inline_keyboard": [[{"text": "📊 Mini App Paneli Aç", "web_app": {"url": PUBLIC_URL}}]]}
                            send_telegram_message("📱 <b>Telegram Mini App Paneli:</b>\n\nAşağıdaki butona dokunarak paneli doğrudan Telegram içinde açabilirsiniz:", chat_id=from_chat_id, reply_markup=markup)
                        else:
                            send_telegram_message("Telegram Mini App adresi henüz ayarlanmamış.", chat_id=from_chat_id)

        except Exception as e:
            logger.error(f"Update dinleme hatası: {e}", exc_info=True)
            time.sleep(3)


# ── Sheet Kontrol Döngüsü ───────────────────────────────────────────────────

def check_and_send_sheet(sheet_config: dict, force_resend: bool = False):
    sheet_name = sheet_config.get("name", "E-Tablo")
    sheet_url = sheet_config.get("url", "")
    sheet_id = sheet_config.get("id") or extract_sheet_id(sheet_url)

    logger.info(f"── Sheet kontrol ediliyor: {sheet_name} (force_resend={force_resend}) ──")

    target_chat = str(sheet_config.get("chat_id") or get_main_chat_id() or TELEGRAM_CHAT_ID or "-5529859923")

    try:
        rows = fetch_sheet_data(sheet_id)
    except Exception as e:
        logger.error(f"[{sheet_name}] Veri çekilemedi: {e}")
        update_sheet_meta(sheet_id, 0, f"Hata: {str(e)[:35]}")
        if force_resend:
            send_telegram_message(f"⚠️ <b>{html.escape(sheet_name)}</b> tablosuna erişilemedi: {html.escape(str(e))}", chat_id=target_chat)
        return

    total_rows = len(rows)
    update_sheet_meta(sheet_id, total_rows, "Aktif")

    if not rows:
        if force_resend:
            send_telegram_message(f"ℹ️ <b>{html.escape(sheet_name)}</b> tablosunda satır bulunamadı (Tablo boş).", chat_id=target_chat)
        return

    headers = list(rows[0].keys())
    col_mapping = find_column_mapping(headers)

    if not col_mapping:
        logger.error(f"[{sheet_name}] Hedef kolon bulunamadı!")
        update_sheet_meta(sheet_id, total_rows, "Kolon hatası")
        if force_resend:
            send_telegram_message(f"⚠️ <b>{html.escape(sheet_name)}</b> tablosunda gerekli kolonlar (Telefon, T.C. vb.) bulunamadı.", chat_id=target_chat)
        return

    last_sent = 0 if force_resend else get_last_sent(sheet_id)
    if not force_resend and last_sent >= total_rows:
        return

    new_rows = rows[last_sent:] if not force_resend else rows
    start_idx = 0 if force_resend else last_sent

    if not new_rows:
        if force_resend:
            send_telegram_message(f"ℹ️ <b>{html.escape(sheet_name)}</b> tablosundaki tüm kayıtlar ({total_rows}/{total_rows}) zaten aktarılmış durumda.", chat_id=target_chat)
        return

    logger.info(f"[{sheet_name}] {len(new_rows)} satır {target_chat} grubuna gönderiliyor...")

    sent_count = 0
    for i, row in enumerate(new_rows):
        entry_number = start_idx + i
        global_lead_id = get_next_global_id()

        message = format_message(global_lead_id, row, col_mapping, sheet_name)
        keyboard = build_record_keyboard(sheet_id, entry_number)

        phone = row.get(col_mapping.get("phone_number", ""), "")
        tc_no = row.get(col_mapping.get("t.c_numaranız", ""), "")

        success, msg_id = send_telegram_message(message, chat_id=target_chat, reply_markup=keyboard)
        if success:
            record_message_sent(sheet_id, entry_number, msg_id or 0, phone=phone, tc_no=tc_no, global_id=global_lead_id)
            sent_count += 1
            time.sleep(1)
        else:
            logger.error(f"[{sheet_name}] Satır #{entry_number} {target_chat} grubuna gönderilemedi.")
            break

    if sent_count > 0:
        logger.info(f"[{sheet_name}] {sent_count} adet kayıt {target_chat} grubuna başarıyla aktarıldı.")
        send_telegram_message(
            f"✅ <b>{html.escape(sheet_name)}</b> tablosundan <b>{sent_count} adet kayıt</b> <code>{target_chat}</code> grubuna başarıyla aktarıldı!",
            chat_id=target_chat
        )


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
