"""
Veri ve Durum Yönetimi
======================
- Google Sheets linklerinin eklenmesi, silinmesi, aktif/pasif yapılması
- Gönderilen Telegram mesaj ID'lerinin kaydı ve silinmesi
- Panelden silinen kayıtların takibi
- PIN brute-force koruması (Rate Limit)
"""

import json
import os
import re
import time
import threading
import logging
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    GOOGLE_SHEETS as DEFAULT_SHEETS,
)

logger = logging.getLogger(__name__)

# Dosya yolları
SHEETS_FILE = "sheets_config.json"
STATE_FILE = "sheets_state.json"
STORE_LOCK = threading.Lock()


# ── URL ve Sheet ID Yardımcıları ──────────────────────────────────────────

def extract_sheet_id(url: str) -> str:
    """Google Sheets URL'sinden ID'yi çıkarır."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        raise ValueError("Geçersiz Google Sheets linki. Lütfen tam linki yapıştırın.")
    return match.group(1)


# ── Google Sheets Yapılandırma Yönetimi ──────────────────────────────────────

def get_sheets() -> list[dict]:
    """Kayıtlı sheet listesini döndürür."""
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            # Varsayılan sheets'i kaydet
            initial = []
            for s in DEFAULT_SHEETS:
                try:
                    s_id = extract_sheet_id(s["url"])
                    initial.append({
                        "id": s_id,
                        "name": s["name"],
                        "url": s["url"],
                        "active": True,
                        "status": "Bekliyor",
                        "last_check": "—",
                        "count": 0
                    })
                except Exception:
                    pass
            save_sheets_unlocked(initial)
            return initial

        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def save_sheets_unlocked(sheets: list[dict]):
    with open(SHEETS_FILE, "w", encoding="utf-8") as f:
        json.dump(sheets, f, ensure_ascii=False, indent=2)


def add_sheet(name: str, url: str) -> tuple[bool, str]:
    """Yeni Google Sheet ekler."""
    try:
        sheet_id = extract_sheet_id(url)
    except ValueError as e:
        return False, str(e)

    with STORE_LOCK:
        sheets = []
        if os.path.exists(SHEETS_FILE):
            try:
                with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                    sheets = json.load(f)
            except Exception:
                sheets = []

        # Zaten var mı kontrol et
        for s in sheets:
            if s.get("id") == sheet_id:
                return False, "Bu Google Sheets zaten ekli!"

        new_entry = {
            "id": sheet_id,
            "name": name.strip() or f"Form {len(sheets) + 1}",
            "url": url.strip(),
            "active": True,
            "status": "Aktif",
            "last_check": "—",
            "count": 0,
        }
        sheets.append(new_entry)
        save_sheets_unlocked(sheets)
        return True, "Google Sheet başarıyla eklendi."


def delete_sheet(sheet_id: str) -> bool:
    """Sheet'i sistemden kaldırır."""
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            return False
        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                sheets = json.load(f)
            sheets = [s for s in sheets if s.get("id") != sheet_id]
            save_sheets_unlocked(sheets)
            return True
        except Exception as e:
            logger.error(f"Sheet silme hatası: {e}")
            return False


def toggle_sheet_active(sheet_id: str) -> bool:
    """Sheet'in aktif/pasif durumunu değiştirir."""
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            return False
        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                sheets = json.load(f)
            for s in sheets:
                if s.get("id") == sheet_id:
                    s["active"] = not s.get("active", True)
                    break
            save_sheets_unlocked(sheets)
            return True
        except Exception as e:
            logger.error(f"Sheet toggle hatası: {e}")
            return False


def update_sheet_meta(sheet_id: str, count: int, status: str = "Aktif"):
    """Sheet'in son durum ve kayıt sayısını günceller."""
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            return
        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                sheets = json.load(f)
            for s in sheets:
                if s.get("id") == sheet_id:
                    s["count"] = count
                    s["status"] = status
                    s["last_check"] = time.strftime("%d/%m/%Y %H:%M:%S")
                    break
            save_sheets_unlocked(sheets)
        except Exception:
            pass


# ── State & Kayıt & Mesaj Yönetimi ──────────────────────────────────────────

def load_state() -> dict:
    """State dosyasını okur."""
    with STORE_LOCK:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_state(state: dict):
    """State dosyasını yazar."""
    with STORE_LOCK:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def get_last_sent(sheet_id: str) -> int:
    state = load_state()
    return state.get(sheet_id, {}).get("last_sent", 0)


def record_message_sent(sheet_id: str, row_num: int, message_id: int):
    """Gönderilen kaydı ve Telegram message_id'sini kaydeder."""
    state = load_state()
    if sheet_id not in state:
        state[sheet_id] = {"last_sent": 0, "messages": {}, "deleted": []}

    sheet_st = state[sheet_id]
    if "messages" not in sheet_st:
        sheet_st["messages"] = {}
    if "deleted" not in sheet_st:
        sheet_st["deleted"] = []

    sheet_st["last_sent"] = max(sheet_st.get("last_sent", 0), row_num + 1)
    sheet_st["messages"][str(row_num)] = message_id
    save_state(state)


def is_record_deleted(sheet_id: str, row_num: int) -> bool:
    """Bir kaydın panelden silinip silinmediğini kontrol eder."""
    state = load_state()
    deleted_list = state.get(sheet_id, {}).get("deleted", [])
    return int(row_num) in deleted_list


def delete_record(sheet_id: str, row_num: int) -> tuple[bool, str]:
    """
    Kaydı panelden siler ve Telegram grubundan da siler.
    """
    state = load_state()
    sheet_st = state.setdefault(sheet_id, {"last_sent": 0, "messages": {}, "deleted": []})
    deleted_list = sheet_st.setdefault("deleted", [])

    row_int = int(row_num)
    if row_int not in deleted_list:
        deleted_list.append(row_int)

    msg_id = sheet_st.get("messages", {}).get(str(row_num))
    tg_deleted = False

    # Telegram'dan mesajı sil
    if msg_id:
        try:
            api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
            resp = requests.post(api_url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": msg_id
            }, timeout=8)
            tg_deleted = resp.json().get("ok", False)
            if tg_deleted:
                logger.info(f"Telegram mesajı #{msg_id} silindi.")
        except Exception as e:
            logger.error(f"Telegram mesaj silme hatası: {e}")

    save_state(state)
    return True, f"Kayıt silindi{' ve Telegram mesajı kaldırıldı.' if tg_deleted else '.'}"


# ── Brute-Force Rate Limiting (PIN Girişi) ───────────────────────────────────

# {ip: {"attempts": int, "lock_until": float}}
RATE_LIMIT_CACHE = {}
RATE_LOCK = threading.Lock()
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 600  # 10 dakika


def check_rate_limit(ip: str) -> tuple[bool, int, int]:
    """
    Kullanıcının PIN deneme izni olup olmadığını kontrol eder.
    Döner: (allowed, remaining_attempts, wait_seconds)
    """
    now = time.time()
    with RATE_LOCK:
        data = RATE_LIMIT_CACHE.get(ip)
        if not data:
            return True, MAX_ATTEMPTS, 0

        # Kilit süresi doldu mu?
        lock_until = data.get("lock_until", 0)
        if lock_until > now:
            wait_sec = int(lock_until - now)
            return False, 0, wait_sec

        # Kilit süresi geçtiyse sıfırla
        if lock_until != 0 and lock_until <= now:
            RATE_LIMIT_CACHE.pop(ip, None)
            return True, MAX_ATTEMPTS, 0

        attempts = data.get("attempts", 0)
        remaining = max(0, MAX_ATTEMPTS - attempts)
        return True, remaining, 0


def record_failed_attempt(ip: str) -> tuple[int, int]:
    """
    Hatalı PIN denemesini kaydeder.
    Döner: (remaining_attempts, wait_seconds)
    """
    now = time.time()
    with RATE_LOCK:
        data = RATE_LIMIT_CACHE.setdefault(ip, {"attempts": 0, "lock_until": 0})
        data["attempts"] += 1

        if data["attempts"] >= MAX_ATTEMPTS:
            data["lock_until"] = now + LOCKOUT_SECONDS
            return 0, LOCKOUT_SECONDS

        remaining = MAX_ATTEMPTS - data["attempts"]
        return remaining, 0


def record_successful_login(ip: str):
    """Başarılı girişte IP sayacını temizler."""
    with RATE_LOCK:
        RATE_LIMIT_CACHE.pop(ip, None)
