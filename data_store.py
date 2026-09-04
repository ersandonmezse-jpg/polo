"""
Veri ve Durum Yönetimi
======================
- Google Sheets linklerinin eklenmesi, silinmesi, aktif/pasif yapılması
- Grup Yönetimi (Aktarılacak hedef gruplar)
- Kayıt durumları (Olumlu, Olumsuz, Kredi Düştü, Cevapsız)
- Gönderilen Telegram mesaj ID'lerinin kaydı ve silinmesi
- Panelden silinen kayıtların takibi
- PIN brute-force koruması (Rate Limit)
"""

import json
import os
import re
import time
from datetime import datetime
import threading
import logging
import requests
import pytz

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    GOOGLE_SHEETS as DEFAULT_SHEETS,
)

logger = logging.getLogger(__name__)

TURKEY_TZ = pytz.timezone("Europe/Istanbul")

# Kalıcı Veri Klasörü (Railway Volume desteği: RAILWAY_VOLUME_MOUNT_PATH veya DATA_DIR veya /app/data)
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR")
if not DATA_DIR:
    if os.path.exists("/data"):
        DATA_DIR = "/data"
    else:
        DATA_DIR = os.path.dirname(os.path.abspath(__file__))

os.makedirs(DATA_DIR, exist_ok=True)

# Dosya yolları (Kalıcı dizine bağlanır)
SHEETS_FILE = os.path.join(DATA_DIR, "sheets_config.json")
GROUPS_FILE = os.path.join(DATA_DIR, "groups_config.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings_config.json")
STATE_FILE = os.path.join(DATA_DIR, "sheets_state.json")
CLIENTS_FILE = os.path.join(DATA_DIR, "clients_db.json")
ACTIVITY_LOG_FILE = os.path.join(DATA_DIR, "activity_log.json")
USERS_FILE = os.path.join(DATA_DIR, "users_db.json")
STORE_LOCK = threading.Lock()


def atomic_save_json(filepath: str, data: dict | list):
    """Veriyi önce geçici bir dosyaya yazar, ardından atomik olarak asıl dosyayla değiştirir."""
    tmp_path = f"{filepath}.tmp_{os.getpid()}_{time.time()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        # Windows dosya kilidi durumunda doğrudan yazma fallback'i
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            logger.error(f"JSON kaydetme hatası ({filepath}): {e2}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ── Telefon & Müşteri Geçmiş Veritabanı (CRM & Mükerrer Başvuru) ──────────────

def normalize_phone(phone_raw: str) -> str:
    """Telefon numarasını standart 10 haneli (örn: 5321234567) formata getirir."""
    if not phone_raw:
        return ""
    digits = re.sub(r"\D", "", str(phone_raw))
    if digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits


def load_clients_db() -> dict:
    with STORE_LOCK:
        try:
            if os.path.exists(CLIENTS_FILE):
                with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}


def save_clients_db(db: dict):
    with STORE_LOCK:
        atomic_save_json(CLIENTS_FILE, db)


def save_client_profile(phone: str, tc_no: str, extra_data: dict, status: str = "", note: str = ""):
    """Müşterinin başvurusunu veya son durumunu telefon ve TC bazlı kalıcı veri tabanına kazır."""
    clean_phone = normalize_phone(phone)
    if not clean_phone and not tc_no:
        return

    primary_key = clean_phone or str(tc_no).strip()
    db = load_clients_db()

    now_ts = time.time()
    now_date_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    profile = db.get(primary_key, {
        "phone": clean_phone,
        "tc_no": str(tc_no).strip(),
        "first_seen": now_date_str,
        "first_timestamp": now_ts,
        "history": [],
        "last_status": "",
        "last_note": "",
        "last_updated": now_date_str
    })

    if status:
        profile["last_status"] = status
    if note:
        profile["last_note"] = note
    profile["last_updated"] = now_date_str

    # Başvuru / Etkileşim geçmişine ekle
    history_entry = {
        "date": now_date_str,
        "timestamp": now_ts,
        "status": status or profile.get("last_status", "Yeni Başvuru"),
        "note": note,
        "details": extra_data
    }
    profile.setdefault("history", []).append(history_entry)

    db[primary_key] = profile
    save_clients_db(db)


def check_client_history(phone: str, tc_no: str) -> dict | None:
    """Telefon veya TC ile daha önce başvuru yapılmış mı kontrol eder."""
    clean_phone = normalize_phone(phone)
    db = load_clients_db()

    profile = None
    if clean_phone and clean_phone in db:
        profile = db[clean_phone]
    elif tc_no and str(tc_no).strip() in db:
        profile = db[str(tc_no).strip()]

    if not profile or not profile.get("history"):
        return None

    # En son veya olumsuz/bloke durumunu analiz et
    last_status = profile.get("last_status", "")
    last_note = profile.get("last_note", "")
    first_seen = profile.get("first_seen", "")
    last_updated = profile.get("last_updated", "")
    total_apps = len(profile.get("history", []))

    # Gün farkı
    first_ts = profile.get("first_timestamp", time.time())
    days_ago = int((time.time() - first_ts) // 86400)

    return {
        "is_returning": total_apps > 1,
        "total_apps": total_apps,
        "first_seen": first_seen,
        "last_updated": last_updated,
        "days_ago": days_ago,
        "last_status": last_status,
        "last_note": last_note,
    }


# ── Aktivite & Raporlama Hafızası (Dashboard KPI) ───────────────────────────

def log_activity_event(event_type: str, sheet_id: str, row_num: int, group_name: str = "", user_name: str = "", amount: float = 0.0, extra: str = ""):
    """Her aranma, kredi düşme, onay, bloke veya olumsuz etkileşimini tarih damgalı kaydeder."""
    with STORE_LOCK:
        events = []
        try:
            if os.path.exists(ACTIVITY_LOG_FILE):
                with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
        except Exception:
            events = []

        now_dt = datetime.now()
        events.append({
            "type": event_type,  # 'call', 'kredi', 'onay', 'bloke', 'olumsuz', 'cevapsiz', 'forward'
            "sheet_id": sheet_id,
            "row_num": row_num,
            "group_name": group_name,
            "user_name": user_name,
            "amount": amount,
            "extra": extra,
            "timestamp": now_dt.timestamp(),
            "date": now_dt.strftime("%Y-%m-%d"),
            "time": now_dt.strftime("%H:%M:%S")
        })

        # Son 10,000 olayı sakla
        if len(events) > 10000:
            events = events[-10000:]

        atomic_save_json(ACTIVITY_LOG_FILE, events)


def get_dashboard_metrics(start_date_str: str = None, end_date_str: str = None) -> dict:
    """Tarih filtresine göre KPI metriklerini hesaplar (Bugün, Dün, Belirli Tarih Aralığı)."""
    with STORE_LOCK:
        events = []
        try:
            if os.path.exists(ACTIVITY_LOG_FILE):
                with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
        except Exception:
            events = []

    # Tarih filtresi uygula (YYYY-MM-DD)
    if start_date_str and end_date_str:
        filtered = [e for e in events if start_date_str <= e.get("date", "") <= end_date_str]
    elif start_date_str:
        filtered = [e for e in events if e.get("date", "") == start_date_str]
    else:
        # Varsayılan: Bugün
        today_str = datetime.now().strftime("%Y-%m-%d")
        filtered = [e for e in events if e.get("date", "") == today_str]

    total_data_worked = len(filtered)
    groups_active = set(e.get("group_name") for e in filtered if e.get("group_name"))
    
    kredi_events = [e for e in filtered if e.get("type") == "kredi"]
    kredi_count = len(kredi_events)
    kredi_total_amt = sum(e.get("amount", 0.0) for e in kredi_events)

    onay_events = [e for e in filtered if e.get("type") == "onay"]
    onay_count = len(onay_events)
    onay_total_amt = sum(e.get("amount", 0.0) for e in onay_events)

    bloke_events = [e for e in filtered if e.get("type") == "bloke"]
    bloke_count = len(bloke_events)

    olumsuz_events = [e for e in filtered if e.get("type") in ("olumsuz", "kacti")]
    olumsuz_count = len(olumsuz_events)

    cevapsiz_events = [e for e in filtered if e.get("type") == "cevapsiz"]
    cevapsiz_count = len(cevapsiz_events)

    return {
        "filter_start": start_date_str or datetime.now().strftime("%Y-%m-%d"),
        "filter_end": end_date_str or datetime.now().strftime("%Y-%m-%d"),
        "total_data_worked": total_data_worked,
        "active_groups_count": len(groups_active),
        "kredi_count": kredi_count,
        "kredi_total_amt": kredi_total_amt,
        "onay_count": onay_count,
        "onay_total_amt": onay_total_amt,
        "bloke_count": bloke_count,
        "olumsuz_count": olumsuz_count,
        "cevapsiz_count": cevapsiz_count,
        "groups_list": list(groups_active)
    }


def record_user_interaction(user_id: int | str, username: str = "", full_name: str = "", action_type: str = "interaction", details: str = ""):
    """Bota etkileşim veren kullanıcıyı ve yaptığı işlemi kayıt altına alır."""
    if not user_id:
        return
    user_key = str(user_id)
    u_name = f"@{username.lstrip('@')}" if username else ""
    now_dt = datetime.now(TURKEY_TZ)
    now_str = now_dt.strftime("%d/%m/%Y %H:%M:%S")

    with STORE_LOCK:
        users = {}
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        users = data
            except Exception:
                users = {}

        if user_key not in users:
            users[user_key] = {
                "user_id": user_key,
                "username": u_name,
                "full_name": full_name,
                "first_seen": now_str,
                "last_seen": now_str,
                "total_actions": 0,
                "actions": {},
                "recent_actions": []
            }

        usr = users[user_key]
        if u_name:
            usr["username"] = u_name
        if full_name:
            usr["full_name"] = full_name
        usr["last_seen"] = now_str
        usr["total_actions"] = usr.get("total_actions", 0) + 1

        actions_map = usr.get("actions", {})
        actions_map[action_type] = actions_map.get(action_type, 0) + 1
        usr["actions"] = actions_map

        recent = usr.get("recent_actions", [])
        recent.insert(0, {
            "type": action_type,
            "details": details[:100] if details else "",
            "time": now_str
        })
        usr["recent_actions"] = recent[:20]

        atomic_save_json(USERS_FILE, users)


def get_all_users() -> list[dict]:
    """Tüm kayıtlı kullanıcıları ve etkileşim sayılarını döner."""
    with STORE_LOCK:
        if not os.path.exists(USERS_FILE):
            return []
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    user_list = list(data.values())
                    user_list.sort(key=lambda x: x.get("total_actions", 0), reverse=True)
                    return user_list
        except Exception:
            return []
    return []


def get_today_summary() -> dict:
    """Gün içinde gerçekleşen tüm hareketlerin ve etkileşim verenlerin özetini döner."""
    now_dt = datetime.now(TURKEY_TZ)
    today_str = now_dt.strftime("%Y-%m-%d")

    with STORE_LOCK:
        events = []
        if os.path.exists(ACTIVITY_LOG_FILE):
            try:
                with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []

    # Bugün gerçekleşen olayları filtrele
    today_events = [e for e in events if e.get("date") == today_str or (e.get("time") and not e.get("date"))]
    today_events.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    # İstatistikler
    kredi_events = [e for e in today_events if e.get("type") == "kredi"]
    onay_events = [e for e in today_events if e.get("type") == "onay"]
    bloke_events = [e for e in today_events if e.get("type") == "bloke"]
    olumsuz_events = [e for e in today_events if e.get("type") in ("olumsuz", "kacti")]
    cevapsiz_events = [e for e in today_events if e.get("type") == "cevapsiz"]
    forward_events = [e for e in today_events if e.get("type") == "forward"]

    # Kullanıcı bazında bugün kaç işlem yapıldı
    user_counts = {}
    for e in today_events:
        uname = e.get("user_name") or "Bilinmeyen"
        user_counts[uname] = user_counts.get(uname, 0) + 1

    top_users = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)

    # Formatlanmış son olaylar (akış için)
    feed = []
    type_labels = {
        "kredi": ("💳 Kredi Düştü", "#facc15"),
        "onay": ("✅ Onaylandı", "#4ade80"),
        "bloke": ("🚫 Bloke Oldu", "#ef4444"),
        "olumsuz": ("🔴 Olumsuz", "#ef4444"),
        "kacti": ("❌ İşlem Kaçtı", "#f97316"),
        "cevapsiz": ("📵 Cevapsız", "#94a3b8"),
        "forward": ("↪️ Gruba Aktarıldı", "#38bdf8"),
        "finish": ("🏁 İşlem Bitti", "#10b981"),
        "note": ("📝 Not Eklendi", "#a855f7"),
    }

    for e in today_events[:30]:
        etype = e.get("type", "")
        lbl, color = type_labels.get(etype, (etype.capitalize(), "#94a3b8"))
        feed.append({
            "time": e.get("time", ""),
            "type_label": lbl,
            "type_color": color,
            "user": e.get("user_name", "Temsilci"),
            "row_num": e.get("row_num", 0),
            "sheet_id": e.get("sheet_id", ""),
            "amount": e.get("amount", 0.0),
            "group": e.get("group_name", ""),
            "extra": e.get("extra", "")
        })

    return {
        "today_date": now_dt.strftime("%d.%m.%Y"),
        "total_actions": len(today_events),
        "kredi_count": len(kredi_events),
        "kredi_amt": sum(e.get("amount", 0.0) for e in kredi_events),
        "onay_count": len(onay_events),
        "onay_amt": sum(e.get("amount", 0.0) for e in onay_events),
        "bloke_count": len(bloke_events),
        "olumsuz_count": len(olumsuz_events),
        "cevapsiz_count": len(cevapsiz_events),
        "forward_count": len(forward_events),
        "top_users": top_users,
        "feed": feed
    }


def get_settings() -> dict:
    """Saha grubu ve sahacı listesini okur."""
    with STORE_LOCK:
        if not os.path.exists(SETTINGS_FILE):
            default_settings = {
                "saha_group_id": "",
                "saha_group_name": "Saha Grubu",
                "sahaci_users": [],  # ['@ahmet', '@mehmet']
                "saha_rate": 15.0,    # %15 varsayılan hakediş oranı
            }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(default_settings, f, ensure_ascii=False, indent=2)
            return default_settings

        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "saha_rate" not in data:
                    data["saha_rate"] = 15.0
                return data
        except Exception:
            return {"saha_group_id": "", "saha_group_name": "Saha Grubu", "sahaci_users": [], "saha_rate": 15.0}


def save_settings(settings: dict):
    with STORE_LOCK:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)


def set_saha_rate(rate: float):
    st = get_settings()
    st["saha_rate"] = float(rate)
    save_settings(st)


def set_saha_group(chat_id: str, name: str = "Saha Grubu"):
    st = get_settings()
    st["saha_group_id"] = str(chat_id)
    st["saha_group_name"] = name
    save_settings(st)


DEFAULT_MAIN_CHAT = "-5529859923"


def get_main_chat_id() -> str:
    st = get_settings()
    return str(st.get("main_chat_id") or TELEGRAM_CHAT_ID or DEFAULT_MAIN_CHAT)


def set_main_chat_id(chat_id: str, title: str = "Ana Data Grubu"):
    st = get_settings()
    st["main_chat_id"] = str(chat_id)
    if title:
        st["main_chat_title"] = title
    save_settings(st)


def clean_sahaci_username(username: str) -> str:
    if not username:
        return ""
    u = str(username).strip()
    u = re.sub(r"^https?://t\.me/", "", u)
    u = re.sub(r"^t\.me/", "", u)
    u = u.lstrip("@").rstrip("/.,;: \t\n\r")
    u = re.sub(r"[^a-zA-Z0-9_]", "", u)
    if u:
        return f"@{u}"
    return ""


def add_sahaci_user(username: str) -> str:
    u = clean_sahaci_username(username)
    if not u:
        return ""
    st = get_settings()
    curr_list = st.setdefault("sahaci_users", [])
    if u.lower() not in [x.lower() for x in curr_list]:
        curr_list.append(u)
        save_settings(st)
    return u


def remove_sahaci_user(username: str) -> bool:
    u = clean_sahaci_username(username)
    if not u:
        return False
    st = get_settings()
    curr_list = st.get("sahaci_users", [])
    new_list = [x for x in curr_list if x.lower() != u.lower()]
    if len(new_list) != len(curr_list):
        st["sahaci_users"] = new_list
        save_settings(st)
        return True
    return False


# ── URL ve Sheet ID Yardımcıları ──────────────────────────────────────────

def extract_sheet_id(url_or_id: str) -> str:
    """Google Sheets URL'sinden veya direkt ID'den temiz sheet ID'sini çıkarır."""
    text = (url_or_id or "").strip()
    if re.match(r"^[a-zA-Z0-9-_]{20,}$", text):
        return text

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if match:
        return match.group(1)

    raise ValueError("Geçersiz Google Sheets linki. Lütfen geçerli bir Google E-Tablo linki girin.")


def fetch_google_sheet_title(sheet_id: str) -> str:
    """Google Sheets web sayfasından tablonun gerçek başlığını (Örn: 'zigiligo') çeker."""
    try:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit?usp=sharing"
        res = requests.get(url, timeout=7)
        if res.status_code == 200:
            match = re.search(r"<title>(.*?)</title>", res.text, re.IGNORECASE)
            if match:
                raw_title = match.group(1).strip()
                # ' - Google E-Tablolar' veya ' - Google Sheets' ekini temizle
                clean_title = re.sub(r"\s*-\s*Google\s*(Sheets|E-Tablolar|Drive)?.*$", "", raw_title, flags=re.IGNORECASE).strip()
                if clean_title:
                    return clean_title
    except Exception as e:
        logger.debug(f"Sheet başlığı çekilemedi ({sheet_id}): {e}")
    return ""


# ── Google Sheets Yapılandırma Yönetimi ──────────────────────────────────────

def get_sheets() -> list[dict]:
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            default_list = []
            if DEFAULT_SHEETS:
                for ds in DEFAULT_SHEETS:
                    try:
                        sid = extract_sheet_id(ds["url"])
                        default_list.append({
                            "id": sid,
                            "name": ds.get("name") or fetch_google_sheet_title(sid) or "zigiligo",
                            "url": ds["url"],
                            "chat_id": str(ds.get("chat_id") or get_main_chat_id()),
                            "active": True,
                            "status": "Aktif",
                            "last_check": "—",
                            "count": 0,
                        })
                    except Exception:
                        pass
            save_sheets_unlocked(default_list)
            return default_list

        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    if not data and DEFAULT_SHEETS:
                        for ds in DEFAULT_SHEETS:
                            try:
                                sid = extract_sheet_id(ds["url"])
                                data.append({
                                    "id": sid,
                                    "name": ds.get("name") or fetch_google_sheet_title(sid) or "zigiligo",
                                    "url": ds["url"],
                                    "chat_id": str(ds.get("chat_id") or get_main_chat_id()),
                                    "active": True,
                                    "status": "Aktif",
                                    "last_check": "—",
                                    "count": 0,
                                })
                            except Exception:
                                pass
                        save_sheets_unlocked(data)
                    return data
                return []
        except Exception:
            return []


def save_sheets_unlocked(sheets: list[dict]):
    atomic_save_json(SHEETS_FILE, sheets)


def add_sheet(name: str, url: str, chat_id: str = "") -> tuple[bool, str]:
    try:
        sheet_id = extract_sheet_id(url)
    except ValueError as e:
        return False, str(e)

    clean_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit?usp=sharing"

    # Otomatik gerçek tablo adını çek (Eğer özel isim girilmediyse veya varsayılan 'Form' ise)
    auto_title = fetch_google_sheet_title(sheet_id)
    target_group = str(chat_id or get_main_chat_id())

    with STORE_LOCK:
        sheets = []
        if os.path.exists(SHEETS_FILE):
            try:
                with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        sheets = data
            except Exception:
                sheets = []

        for s in sheets:
            if s.get("id") == sheet_id:
                # İsim ve hedef grup güncellenmek isteniyorsa güncelle
                if auto_title and (s.get("name", "").startswith("Form ") or not s.get("name")):
                    s["name"] = auto_title
                if target_group:
                    s["chat_id"] = target_group
                s["active"] = True
                save_sheets_unlocked(sheets)
                reset_sheet_last_sent(sheet_id)
                return True, f"Tablo hazırlandı: '{s.get('name')}' (Hedef Grup: {target_group})"

        # Kullanıcı özel isim girmediyse ya da 'Form' kaldıysa otomatik tablo başlığını kullan
        user_name = (name or "").strip()
        if not user_name or user_name.lower().startswith("form"):
            sheet_name = auto_title or user_name or f"Form {len(sheets) + 1}"
        else:
            sheet_name = user_name

        new_entry = {
            "id": sheet_id,
            "name": sheet_name,
            "url": clean_url,
            "chat_id": target_group,
            "active": True,
            "status": "Aktif",
            "last_check": "—",
            "count": 0,
        }
        sheets.append(new_entry)
        save_sheets_unlocked(sheets)
        return True, f"'{sheet_name}' başarıyla eklendi. (Kayıtlar {target_group} grubuna iletilecek)"


def delete_sheet(sheet_id: str) -> bool:
    with STORE_LOCK:
        if not os.path.exists(SHEETS_FILE):
            return False
        try:
            with open(SHEETS_FILE, "r", encoding="utf-8") as f:
                sheets = json.load(f)
            sheets = [s for s in sheets if s.get("id") != sheet_id]
            save_sheets_unlocked(sheets)

            # State dosyasından da bu sheet'i tamamen temizle
            try:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        st = json.load(f)
                    st.pop(sheet_id, None)
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            return True
        except Exception as e:
            logger.error(f"Sheet silme hatası: {e}")
            return False


def toggle_sheet_active(sheet_id: str) -> bool:
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


# ── Hedef Grup Yönetimi ─────────────────────────────────────────────────────

def get_groups() -> list[dict]:
    """Aktarılacak kayıtlı hedef grupları döndürür."""
    with STORE_LOCK:
        if not os.path.exists(GROUPS_FILE):
            # Varsayılan ana grubu ekle
            initial = []
            if TELEGRAM_CHAT_ID:
                initial.append({
                    "id": str(TELEGRAM_CHAT_ID),
                    "name": "Ana Grup",
                })
            with open(GROUPS_FILE, "w", encoding="utf-8") as f:
                json.dump(initial, f, ensure_ascii=False, indent=2)
            return initial

        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []


def add_group(name: str, chat_id: str) -> tuple[bool, str]:
    """Yeni hedef grup ekler."""
    c_id = str(chat_id or "").strip()
    g_name = (name or "").strip() or f"Grup ({c_id})"

    if not c_id:
        return False, "Geçersiz Chat ID!"

    with STORE_LOCK:
        groups = []
        if os.path.exists(GROUPS_FILE):
            try:
                with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                    groups = json.load(f)
            except Exception:
                groups = []

        for g in groups:
            if str(g.get("id")) == c_id:
                return False, "Bu grup zaten kayıtlı!"

        groups.append({"id": c_id, "name": g_name})
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(groups, f, ensure_ascii=False, indent=2)
        return True, f"'{g_name}' başarıyla eklendi."


def delete_group(chat_id: str) -> bool:
    """Grubu siler."""
    c_id = str(chat_id).strip()
    with STORE_LOCK:
        if not os.path.exists(GROUPS_FILE):
            return False
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                groups = json.load(f)
            groups = [g for g in groups if str(g.get("id")) != c_id]
            with open(GROUPS_FILE, "w", encoding="utf-8") as f:
                json.dump(groups, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


# ── State & Kayıt & Durum Yönetimi ──────────────────────────────────────────

def load_state() -> dict:
    with STORE_LOCK:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_state(state: dict):
    with STORE_LOCK:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def get_last_sent(sheet_id: str) -> int:
    state = load_state()
    return state.get(sheet_id, {}).get("last_sent", 0)


def reset_sheet_last_sent(sheet_id: str = ""):
    """Belirtilen sheet'in (veya tüm sheet'lerin) last_sent sayacını sıfırlar."""
    with STORE_LOCK:
        state = load_state()
        if sheet_id:
            if sheet_id in state and isinstance(state[sheet_id], dict):
                state[sheet_id]["last_sent"] = 0
        else:
            for sid in state:
                if isinstance(state[sid], dict) and "last_sent" in state[sid]:
                    state[sid]["last_sent"] = 0
        save_state(state)



def get_next_global_id() -> int:
    """Tüm sheetler boyunca kümülatif artan global kayıt numarasını (Örn: #1, #2, #550) döner ve sayacı 1 artırır."""
    state = load_state()
    current_counter = state.get("__global_lead_counter__", 0)
    next_counter = current_counter + 1
    state["__global_lead_counter__"] = next_counter
    save_state(state)
    return next_counter


def record_message_sent(sheet_id: str, row_num: int, message_id: int, phone: str = "", tc_no: str = "", global_id: int = None):
    state = load_state()
    if sheet_id not in state:
        state[sheet_id] = {"last_sent": 0, "messages": {}, "statuses": {}, "clients": {}, "global_ids": {}, "deleted": []}

    sheet_st = state[sheet_id]
    if "messages" not in sheet_st:
        sheet_st["messages"] = {}
    if "statuses" not in sheet_st:
        sheet_st["statuses"] = {}
    if "clients" not in sheet_st:
        sheet_st["clients"] = {}
    if "global_ids" not in sheet_st:
        sheet_st["global_ids"] = {}
    if "deleted" not in sheet_st:
        sheet_st["deleted"] = []

    sheet_st["last_sent"] = max(sheet_st.get("last_sent", 0), row_num + 1)
    sheet_st["messages"][str(row_num)] = message_id
    if global_id:
        sheet_st["global_ids"][str(row_num)] = global_id
        state["__global_lead_counter__"] = max(state.get("__global_lead_counter__", 0), global_id)

    if phone or tc_no:
        sheet_st["clients"][str(row_num)] = {"phone": phone, "tc_no": tc_no}
    save_state(state)


def get_record_global_id(sheet_id: str, row_num: int) -> int:
    """Kaydın global kayıt numarasını döner, yoksa yerel row_num döner."""
    state = load_state()
    g_id = state.get(sheet_id, {}).get("global_ids", {}).get(str(row_num))
    return int(g_id) if g_id else (row_num + 1)


def set_record_status(sheet_id: str, row_num: int, status: str, user_name: str = "", note: str = ""):
    """Kaydın durumunu günceller ve müşterinin kalıcı CRM profiline de yansıtır."""
    state = load_state()
    sheet_st = state.setdefault(sheet_id, {"last_sent": 0, "messages": {}, "statuses": {}, "clients": {}, "forwarded": {}, "deleted": []})
    statuses = sheet_st.setdefault("statuses", {})
    statuses[str(row_num)] = {
        "status": status,
        "user": user_name,
        "note": note,
        "time": time.strftime("%H:%M")
    }
    save_state(state)

    # MÜŞTERİ KALICI CRM PROFİLİNİ ANINDA GÜNCELLE
    try:
        client_info = sheet_st.get("clients", {}).get(str(row_num), {})
        phone = client_info.get("phone", "")
        tc_no = client_info.get("tc_no", "")
        if phone or tc_no:
            save_client_profile(phone, tc_no, extra_data={"sheet_id": sheet_id, "row_num": row_num}, status=status, note=note)
    except Exception as e:
        logger.error(f"CRM senkronizasyon hatası: {e}")


def get_record_status(sheet_id: str, row_num: int) -> dict:
    state = load_state()
    return state.get(sheet_id, {}).get("statuses", {}).get(str(row_num), {})


def get_original_message_id(sheet_id: str, row_num: int) -> int | None:
    """Kaydın ana gruptaki orijinal mesaj ID'sini döner."""
    state = load_state()
    msg_id = state.get(sheet_id, {}).get("messages", {}).get(str(row_num))
    return int(msg_id) if msg_id else None


def record_forward_event(sheet_id: str, row_num: int, target_chat_id: str, target_chat_name: str, target_msg_id: int, user_name: str):
    """Kaydın bir gruba aktarıldığını ve aktarım saatini kaydeder."""
    state = load_state()
    sheet_st = state.setdefault(sheet_id, {"last_sent": 0, "messages": {}, "statuses": {}, "forwarded": {}, "deleted": []})
    fwd_dict = sheet_st.setdefault("forwarded", {})
    fwd_dict[str(row_num)] = {
        "target_chat_id": str(target_chat_id),
        "target_chat_name": target_chat_name,
        "target_msg_id": target_msg_id,
        "fwd_user": user_name,
        "fwd_timestamp": time.time(),
        "fwd_time_str": time.strftime("%H:%M:%S"),
    }
    save_state(state)


def get_forward_event(sheet_id: str, row_num: int) -> dict | None:
    state = load_state()
    return state.get(sheet_id, {}).get("forwarded", {}).get(str(row_num))


def format_duration(seconds: float) -> str:
    """Saniyeyi insansı 'X Saat Y Dk Z Sn' formatına çevirir."""
    sec = int(seconds)
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    remaining_sec = sec % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} saat")
    if minutes > 0:
        parts.append(f"{minutes} dk")
    parts.append(f"{remaining_sec} sn")
    return " ".join(parts)


# ── Bekleyen Kullanıcı Yanıtları (Not, Tutar, IBAN) ──────────────────────────
# { user_id: {"action": "note|amount|iban", "sheet_id": ..., "row_num": ..., "origin_chat_id": ..., "origin_msg_id": ...} }
PENDING_ACTIONS = {}
PENDING_LOCK = threading.Lock()


def set_pending_action(user_id: int, action_data: dict):
    with PENDING_LOCK:
        PENDING_ACTIONS[user_id] = action_data


def get_pending_action(user_id: int) -> dict | None:
    with PENDING_LOCK:
        return PENDING_ACTIONS.get(user_id)


def clear_pending_action(user_id: int):
    with PENDING_LOCK:
        PENDING_ACTIONS.pop(user_id, None)


def is_record_deleted(sheet_id: str, row_num: int) -> bool:
    state = load_state()
    deleted_list = state.get(sheet_id, {}).get("deleted", [])
    return int(row_num) in deleted_list


def delete_record(sheet_id: str, row_num: int) -> tuple[bool, str]:
    state = load_state()
    sheet_st = state.setdefault(sheet_id, {"last_sent": 0, "messages": {}, "statuses": {}, "deleted": []})
    deleted_list = sheet_st.setdefault("deleted", [])

    row_int = int(row_num)
    if row_int not in deleted_list:
        deleted_list.append(row_int)

    msg_id = sheet_st.get("messages", {}).get(str(row_num))
    tg_deleted = False

    if msg_id and TELEGRAM_BOT_TOKEN:
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

RATE_LIMIT_CACHE = {}
RATE_LOCK = threading.Lock()
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 600  # 10 dakika


def check_rate_limit(ip: str) -> tuple[bool, int, int]:
    now = time.time()
    with RATE_LOCK:
        data = RATE_LIMIT_CACHE.get(ip)
        if not data:
            return True, MAX_ATTEMPTS, 0

        lock_until = data.get("lock_until", 0)
        if lock_until > now:
            wait_sec = int(lock_until - now)
            return False, 0, wait_sec

        if lock_until != 0 and lock_until <= now:
            RATE_LIMIT_CACHE.pop(ip, None)
            return True, MAX_ATTEMPTS, 0

        attempts = data.get("attempts", 0)
        remaining = max(0, MAX_ATTEMPTS - attempts)
        return True, remaining, 0


def record_failed_attempt(ip: str) -> tuple[int, int]:
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
    with RATE_LOCK:
        RATE_LIMIT_CACHE.pop(ip, None)
