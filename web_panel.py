"""
Telegram Mini App — Web Panel
==============================
- PIN korumalı (Brute-force koruması ve Ekran Numpad'i)
- Google Sheets link ekleme / çıkarma / aktiflik yönetimi
- Kayıtları tek tuşla tümüyle kopyalama
- Kayıt silme ve Telegram grubundan da silme (deleteMessage)
"""

import csv
import io
import json
import os
import re
import logging
from datetime import datetime
from functools import wraps

import pytz
import requests
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify

from config import (
    ADMIN_PIN,
    WEB_PORT,
)
from data_store import (
    get_sheets,
    add_sheet,
    delete_sheet,
    toggle_sheet_active,
    extract_sheet_id,
    delete_record,
    is_record_deleted,
    check_rate_limit,
    record_failed_attempt,
    record_successful_login,
    get_groups,
    add_group,
    delete_group,
    get_record_status,
    get_forward_event,
    format_duration,
    get_dashboard_metrics,
    get_record_global_id,
    get_main_chat_id,
    get_today_summary,
    get_all_users,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)

@app.after_request
def allow_telegram_iframe(response):
    # Telegram Web App iframe'i için gerekli izin başlıkları
    response.headers["X-Frame-Options"] = "ALLOWALL"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org telegram:;"
    return response

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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    urls = [
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv",
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    ]
    last_err = None
    for csv_url in urls:
        for attempt in range(2):
            try:
                response = requests.get(csv_url, headers=headers, timeout=20)
                if response.status_code == 200 and response.content:
                    content = response.content.decode("utf-8-sig")
                    return list(csv.DictReader(io.StringIO(content)))
            except Exception as e:
                last_err = e
                time.sleep(1)
    if last_err:
        raise last_err
    return []


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
    normalized = [(h, normalize_tr(h)) for h in headers]

    # 1. Kart Limiti (Kesinlikle 'limit' içeren kolonlar önceliklidir; 'kullanıyor musunuz' gibi soru kolonları hariç tutulur)
    for h, norm in normalized:
        if "limit" in norm:
            mapping["kullanılabilir_kart_limitiniz"] = h
            break
    if "kullanılabilir_kart_limitiniz" not in mapping:
        for h, norm in normalized:
            if "kart" in norm and not any(skip in norm for skip in ["kullaniyor", "misiniz", "musunuz", "var_mi", "sahibi", "hesap"]):
                mapping["kullanılabilir_kart_limitiniz"] = h
                break

    # 2. Telefon Numarası
    for h, norm in normalized:
        if any(k in norm for k in ["telefon", "phone", "gsm", "mobile"]) or norm == "tel":
            mapping["phone_number"] = h
            break

    # 3. T.C. Kimlik
    for h, norm in normalized:
        if any(k in norm for k in ["tc_no", "tc_numara", "tckn", "kimlik"]) or "tc" in norm.split("_"):
            mapping["t.c_numaranız"] = h
            break

    # 4. Çalışma Durumu
    for h, norm in normalized:
        if any(k in norm for k in ["calisma", "meslek", "durum"]):
            mapping["çalışma_durumu"] = h
            break

    # 5. Kayıt Tarihi
    for h, norm in normalized:
        if any(k in norm for k in ["created", "tarih", "zaman"]):
            mapping["created_time"] = h
            break

    return mapping


def convert_to_turkey_time(time_str: str) -> str:
    if not time_str or not time_str.strip():
        return "—"
    time_str = time_str.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = pytz.utc.localize(dt)
            return dt.astimezone(TURKEY_TZ).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
    return time_str


def get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── HTML ŞABLONLARI ─────────────────────────────────────────────────────────

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Admin Giriş</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-color: var(--tg-theme-bg-color, #0c0e14);
            --card-bg: var(--tg-theme-secondary-bg-color, rgba(255,255,255,0.04));
            --text-color: var(--tg-theme-text-color, #f4f4f5);
            --hint-color: var(--tg-theme-hint-color, #71717a);
            --btn-color: var(--tg-theme-button-color, #6366f1);
            --btn-text: var(--tg-theme-button-text-color, #ffffff);
            --border-color: rgba(255,255,255,0.08);
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .login-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 24px 20px;
            width: 100%;
            max-width: 320px;
            text-align: center;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        .lock-icon { font-size: 38px; margin-bottom: 8px; }
        h1 { font-size: 19px; font-weight: 700; margin-bottom: 4px; }
        .subtitle {
            font-size: 12px;
            color: var(--hint-color);
            margin-bottom: 18px;
        }
        .pin-dots {
            display: flex;
            gap: 12px;
            margin-bottom: 18px;
        }
        .pin-dot {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            border: 2px solid rgba(255,255,255,0.25);
            background: transparent;
            transition: all 0.15s ease;
        }
        .pin-dot.filled {
            background: var(--btn-color);
            border-color: var(--btn-color);
            box-shadow: 0 0 10px rgba(99,102,241,0.5);
            transform: scale(1.15);
        }
        .error-msg {
            background: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.3);
            border-radius: 10px;
            padding: 8px 12px;
            font-size: 12px;
            margin-bottom: 14px;
            width: 100%;
        }
        .lockout-msg {
            background: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.3);
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 12px;
            margin-bottom: 14px;
            width: 100%;
            font-weight: 600;
        }

        /* ── On-Screen Numpad ── */
        .numpad {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            width: 100%;
            max-width: 260px;
        }
        .num-btn {
            aspect-ratio: 1;
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            color: var(--text-color);
            font-size: 20px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.1s;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }
        .num-btn:active {
            transform: scale(0.92);
            background: rgba(99,102,241,0.25);
            border-color: var(--btn-color);
        }
        .num-btn.fn-btn {
            font-size: 15px;
            background: rgba(255,255,255,0.03);
            color: var(--hint-color);
        }
        .num-btn:disabled {
            opacity: 0.3;
            pointer-events: none;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="lock-icon">🔐</div>
        <h1>Admin Giriş</h1>
        <p class="subtitle">PIN kodunu tuşlayın</p>

        {% if locked %}
        <div class="lockout-msg">
            ⛔ Çok fazla hatalı deneme!<br>
            Lütfen <b>{{ wait_sec }} saniye</b> sonra tekrar deneyin.
        </div>
        {% elif error %}
        <div class="error-msg">
            {{ error }}
            {% if remaining < 5 %}
            <div style="font-size:11px; margin-top:3px; opacity:0.85;">Kalan deneme hakkı: {{ remaining }}</div>
            {% endif %}
        </div>
        {% endif %}

        <div class="pin-dots">
            <div class="pin-dot" id="dot-0"></div>
            <div class="pin-dot" id="dot-1"></div>
            <div class="pin-dot" id="dot-2"></div>
            <div class="pin-dot" id="dot-3"></div>
        </div>

        <form method="POST" id="pinForm">
            <input type="hidden" name="pin" id="pinInput">
        </form>

        <div class="numpad">
            <button type="button" class="num-btn" onclick="pressKey('1')" {% if locked %}disabled{% endif %}>1</button>
            <button type="button" class="num-btn" onclick="pressKey('2')" {% if locked %}disabled{% endif %}>2</button>
            <button type="button" class="num-btn" onclick="pressKey('3')" {% if locked %}disabled{% endif %}>3</button>
            <button type="button" class="num-btn" onclick="pressKey('4')" {% if locked %}disabled{% endif %}>4</button>
            <button type="button" class="num-btn" onclick="pressKey('5')" {% if locked %}disabled{% endif %}>5</button>
            <button type="button" class="num-btn" onclick="pressKey('6')" {% if locked %}disabled{% endif %}>6</button>
            <button type="button" class="num-btn" onclick="pressKey('7')" {% if locked %}disabled{% endif %}>7</button>
            <button type="button" class="num-btn" onclick="pressKey('8')" {% if locked %}disabled{% endif %}>8</button>
            <button type="button" class="num-btn" onclick="pressKey('9')" {% if locked %}disabled{% endif %}>9</button>
            <button type="button" class="num-btn fn-btn" onclick="clearPin()" {% if locked %}disabled{% endif %}>C</button>
            <button type="button" class="num-btn" onclick="pressKey('0')" {% if locked %}disabled{% endif %}>0</button>
            <button type="button" class="num-btn fn-btn" onclick="backspace()" {% if locked %}disabled{% endif %}>⌫</button>
        </div>
    </div>

    <script>
        if (window.Telegram && Telegram.WebApp) {
            Telegram.WebApp.ready();
            Telegram.WebApp.expand();
        }

        let enteredPin = "";
        const pinInput = document.getElementById("pinInput");
        const form = document.getElementById("pinForm");

        function updateDots() {
            for (let i = 0; i < 4; i++) {
                const dot = document.getElementById("dot-" + i);
                if (i < enteredPin.length) {
                    dot.classList.add("filled");
                } else {
                    dot.classList.remove("filled");
                }
            }
        }

        function triggerHaptic(type = "light") {
            if (window.Telegram && Telegram.WebApp && Telegram.WebApp.HapticFeedback) {
                Telegram.WebApp.HapticFeedback.impactOccurred(type);
            }
        }

        function pressKey(num) {
            if (enteredPin.length < 4) {
                enteredPin += num;
                triggerHaptic("light");
                updateDots();

                if (enteredPin.length === 4) {
                    pinInput.value = enteredPin;
                    setTimeout(() => form.submit(), 150);
                }
            }
        }

        function backspace() {
            if (enteredPin.length > 0) {
                enteredPin = enteredPin.slice(0, -1);
                triggerHaptic("medium");
                updateDots();
            }
        }

        function clearPin() {
            enteredPin = "";
            triggerHaptic("heavy");
            updateDots();
        }

        // Klavye desteği
        document.addEventListener("keydown", (e) => {
            if (e.key >= "0" && e.key <= "9") {
                pressKey(e.key);
            } else if (e.key === "Backspace") {
                backspace();
            } else if (e.key === "Escape") {
                clearPin();
            }
        });
    </script>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Admin Panel</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-color: var(--tg-theme-bg-color, #0c0e14);
            --card-bg: var(--tg-theme-secondary-bg-color, rgba(255,255,255,0.04));
            --text-color: var(--tg-theme-text-color, #f4f4f5);
            --hint-color: var(--tg-theme-hint-color, #71717a);
            --btn-color: var(--tg-theme-button-color, #6366f1);
            --btn-text: var(--tg-theme-button-text-color, #ffffff);
            --border-color: rgba(255,255,255,0.08);
            --accent-glow: rgba(99,102,241,0.15);
            --danger-color: #ef4444;
            --success-color: #22c55e;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            justify-content: center;
        }

        .app-shell {
            width: 100%;
            max-width: 540px;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            padding-bottom: 24px;
        }

        @media (min-width: 768px) {
            .app-shell { max-width: 820px; }
        }

        /* ── Top Navigation Bar ── */
        .top-bar {
            background: rgba(18, 20, 29, 0.9);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border-color);
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .top-title {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 16px;
            font-weight: 700;
        }
        .top-actions {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .icon-btn {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            color: var(--text-color);
            padding: 6px 10px;
            border-radius: 9px;
            display: flex;
            align-items: center;
            gap: 5px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
            text-decoration: none;
            transition: all 0.15s;
        }
        .icon-btn:active {
            transform: scale(0.94);
            background: rgba(255,255,255,0.1);
        }
        .icon-btn.primary {
            background: var(--btn-color);
            color: var(--btn-text);
            border-color: var(--btn-color);
        }

        /* ── KPI & Dashboard Stats Bar ── */
        .date-filter-bar {
            padding: 10px 14px 4px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 8px;
        }
        .filter-buttons {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }
        .f-btn {
            padding: 5px 11px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            background: var(--card-bg);
            color: var(--hint-color);
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.15s;
        }
        .f-btn:hover { background: rgba(255,255,255,0.08); color: #fff; }
        .f-btn.active {
            background: var(--btn-color);
            color: #fff;
            border-color: var(--btn-color);
        }
        .stats-bar {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            padding: 8px 14px 4px;
        }
        @media (min-width: 768px) {
            .stats-bar {
                grid-template-columns: repeat(4, 1fr);
            }
        }
        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .stat-card .s-label {
            font-size: 10px;
            color: var(--hint-color);
            text-transform: uppercase;
            letter-spacing: 0.4px;
            font-weight: 700;
        }
        .stat-card .s-value {
            font-size: 17px;
            font-weight: 800;
            margin-top: 2px;
            color: var(--text-color);
        }
        .stat-card.green { border-color: rgba(34, 197, 94, 0.3); background: rgba(34, 197, 94, 0.05); }
        .stat-card.green .s-value { color: #4ade80; }
        .stat-card.red { border-color: rgba(239, 68, 68, 0.3); background: rgba(239, 68, 68, 0.05); }
        .stat-card.red .s-value { color: #f87171; }
        .stat-card.yellow { border-color: rgba(234, 179, 8, 0.3); background: rgba(234, 179, 8, 0.05); }
        .stat-card.yellow .s-value { color: #facc15; }
        .stat-card.blue { border-color: rgba(99, 102, 241, 0.3); background: rgba(99, 102, 241, 0.05); }
        .stat-card.blue .s-value { color: #818cf8; }

        /* ── Sheet Tabs ── */
        .tabs-scroller {
            display: flex;
            gap: 6px;
            padding: 10px 14px 4px;
            overflow-x: auto;
            scrollbar-width: none;
            -webkit-overflow-scrolling: touch;
        }
        .tabs-scroller::-webkit-scrollbar { display: none; }
        .sheet-tab {
            flex-shrink: 0;
            padding: 7px 14px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            color: var(--hint-color);
            cursor: pointer;
            transition: all 0.15s;
        }
        .sheet-tab.active {
            background: var(--btn-color);
            color: var(--btn-text);
            border-color: var(--btn-color);
            box-shadow: 0 2px 10px var(--accent-glow);
        }

        /* ── Search ── */
        .search-container {
            padding: 10px 14px;
            position: relative;
        }
        .search-input {
            width: 100%;
            padding: 9px 14px 9px 34px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-color);
            font-size: 13px;
            outline: none;
            transition: border-color 0.15s;
        }
        .search-input:focus { border-color: var(--btn-color); }
        .search-input::placeholder { color: #52525b; }
        .search-icon {
            position: absolute;
            left: 24px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 13px;
            color: #71717a;
            pointer-events: none;
        }

        /* ── Cards Grid ── */
        .cards-list {
            padding: 0 14px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        @media (min-width: 768px) {
            .cards-list {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 10px;
            }
        }

        /* ── Individual Card ── */
        .data-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            transition: opacity 0.25s ease, transform 0.25s ease;
        }
        .data-card.removing {
            opacity: 0;
            transform: scale(0.92);
        }

        /* Card Header */
        .card-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding-bottom: 6px;
        }
        .card-id-tag {
            background: var(--accent-glow);
            color: #a5b4fc;
            font-weight: 700;
            font-size: 11px;
            padding: 2px 7px;
            border-radius: 6px;
        }
        .card-date {
            font-size: 11px;
            color: var(--hint-color);
        }

        /* Compact Grid */
        .card-body-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px 10px;
        }
        .field-box {
            display: flex;
            flex-direction: column;
            gap: 2px;
            min-width: 0;
        }
        .field-box.full-width { grid-column: 1 / -1; }
        .f-label {
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.3px;
            color: var(--hint-color);
        }
        .f-val {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-color);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* TC Box */
        .tc-btn {
            background: rgba(99, 102, 241, 0.12);
            color: #a5b4fc;
            border: 1px dashed rgba(99, 102, 241, 0.3);
            border-radius: 7px;
            padding: 4px 8px;
            font-family: 'SF Mono', Consolas, monospace;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.5px;
            display: inline-flex;
            align-items: center;
            justify-content: space-between;
            cursor: pointer;
            width: 100%;
            transition: all 0.15s;
        }
        .tc-btn:active {
            transform: scale(0.97);
            background: rgba(99, 102, 241, 0.25);
        }
        .tc-btn.copied {
            background: rgba(34, 197, 94, 0.2) !important;
            border-color: #22c55e !important;
            color: #4ade80 !important;
        }

        /* Card Action Buttons */
        .card-actions {
            display: flex;
            gap: 6px;
            margin-top: 4px;
            padding-top: 6px;
            border-top: 1px solid rgba(255,255,255,0.05);
        }
        .act-btn {
            flex: 1;
            padding: 6px 8px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid var(--border-color);
            background: rgba(255,255,255,0.04);
            color: var(--text-color);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            transition: all 0.15s;
        }
        .act-btn:active { transform: scale(0.96); }
        .act-btn.copy-all {
            background: rgba(99, 102, 241, 0.12);
            color: #a5b4fc;
            border-color: rgba(99, 102, 241, 0.25);
        }
        .act-btn.copy-all.copied {
            background: rgba(34, 197, 94, 0.2);
            color: #4ade80;
            border-color: #22c55e;
        }
        .act-btn.delete {
            background: rgba(239, 68, 68, 0.1);
            color: #f87171;
            border-color: rgba(239, 68, 68, 0.25);
        }
        .act-btn.delete:hover {
            background: rgba(239, 68, 68, 0.2);
        }

        /* ── Link Management Modal/View ── */
        .links-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(12px);
            z-index: 200;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .links-modal.open { display: flex; }
        .modal-content {
            background: #131620;
            border: 1px solid var(--border-color);
            border-radius: 18px;
            width: 100%;
            max-width: 480px;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.6);
        }
        .modal-head {
            padding: 14px 18px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-head h2 { font-size: 16px; font-weight: 700; }
        .close-btn {
            background: none;
            border: none;
            color: var(--hint-color);
            font-size: 20px;
            cursor: pointer;
            padding: 2px 6px;
        }
        .modal-body {
            padding: 16px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        /* Add Sheet Form */
        .add-sheet-box {
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .add-sheet-box h3 { font-size: 13px; font-weight: 700; color: #a5b4fc; }
        .form-input {
            width: 100%;
            padding: 9px 12px;
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: #fff;
            font-size: 12px;
            outline: none;
        }
        .form-input:focus { border-color: var(--btn-color); }
        .submit-btn {
            padding: 10px;
            background: var(--btn-color);
            color: var(--btn-text);
            border: none;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.15s;
        }
        .submit-btn:active { transform: scale(0.97); }

        /* Sheet Items List */
        .sheet-item {
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .sheet-item-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .sheet-name { font-size: 13px; font-weight: 700; color: #fff; }
        .status-tag {
            font-size: 10px;
            padding: 2px 7px;
            border-radius: 10px;
            font-weight: 700;
        }
        .status-tag.active {
            background: rgba(34, 197, 94, 0.15);
            color: #4ade80;
            border: 1px solid rgba(34, 197, 94, 0.3);
        }
        .status-tag.passive {
            background: rgba(113, 113, 122, 0.15);
            color: #a1a1aa;
            border: 1px solid rgba(113, 113, 122, 0.3);
        }
        .sheet-meta {
            font-size: 11px;
            color: var(--hint-color);
            word-break: break-all;
        }
        .sheet-actions {
            display: flex;
            gap: 8px;
            margin-top: 4px;
        }
        .sm-btn {
            flex: 1;
            padding: 6px;
            border-radius: 7px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid var(--border-color);
            background: rgba(255,255,255,0.05);
            color: var(--text-color);
            transition: all 0.15s;
        }
        .sm-btn.toggle { color: #60a5fa; }
        .sm-btn.del { color: #f87171; border-color: rgba(239, 68, 68, 0.3); }

        /* Toast Message */
        .toast {
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: rgba(18, 20, 29, 0.95);
            border: 1px solid var(--btn-color);
            color: #fff;
            padding: 10px 18px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            box-shadow: 0 8px 25px rgba(0,0,0,0.5);
            z-index: 300;
            transition: transform 0.3s ease;
            pointer-events: none;
            white-space: nowrap;
        }
        .toast.show {
            transform: translateX(-50%) translateY(0);
        }

        .tab-panel { display: none; }
        .tab-panel.active { display: block; }
        .empty-box {
            text-align: center;
            padding: 50px 16px;
            color: var(--hint-color);
        }
        .empty-box .e-icon { font-size: 32px; margin-bottom: 8px; }
    </style>
</head>
<body>
    <div class="app-shell">
        <!-- Top Navigation -->
        <div class="top-bar">
            <div class="top-title">
                <span>📊</span>
                <span>Admin Panel</span>
            </div>
            <div class="top-actions">
                <button class="icon-btn primary" onclick="openLinksModal()" title="Link Yönetimi">🔗 Linkler</button>
                <button class="icon-btn" onclick="openGroupsModal()" title="Hedef Gruplar">👥 Gruplar</button>
                <button class="icon-btn" onclick="location.reload()" title="Yenile">🔄</button>
                <a href="/logout" class="icon-btn" title="Çıkış">🚪</a>
            </div>
        </div>

        <!-- Date Filter Bar -->
        <div class="date-filter-bar">
            <span style="font-size:11px; font-weight:700; color:var(--hint-color); text-transform:uppercase;">📅 Raporlama:</span>
            <div class="filter-buttons">
                <a href="/?filter=today" class="f-btn {% if active_filter == 'today' %}active{% endif %}">Bugün</a>
                <a href="/?filter=yesterday" class="f-btn {% if active_filter == 'yesterday' %}active{% endif %}">Dün</a>
                <a href="/?filter=week" class="f-btn {% if active_filter == 'week' %}active{% endif %}">Bu Hafta</a>
                <a href="/?filter=month" class="f-btn {% if active_filter == 'month' %}active{% endif %}">Bu Ay</a>
                <a href="/?filter=all" class="f-btn {% if active_filter == 'all' %}active{% endif %}">Tüm Zamanlar</a>
            </div>
        </div>

        <!-- Rich KPI Stats Bar -->
        <div class="stats-bar">
            <div class="stat-card blue">
                <div class="s-label">📞 İşlenen Data</div>
                <div class="s-value">{{ kpi.total_data_worked }} <span style="font-size:11px; font-weight:500; color:var(--hint-color);">/ {{ total_rows }}</span></div>
            </div>
            <div class="stat-card">
                <div class="s-label">👥 Aktif Grup</div>
                <div class="s-value">{{ kpi.active_groups_count }} <span style="font-size:11px; font-weight:500; color:var(--hint-color);">Grup</span></div>
            </div>
            <div class="stat-card yellow">
                <div class="s-label">💳 Kredi Düştü</div>
                <div class="s-value">{{ kpi.kredi_count }} <span style="font-size:11px; font-weight:600; color:#facc15;">({{ "{:,.0f}".format(kpi.kredi_total_amt or 0) }} TL)</span></div>
            </div>
            <div class="stat-card green">
                <div class="s-label">✅ Onaylanan</div>
                <div class="s-value">{{ kpi.onay_count }} <span style="font-size:11px; font-weight:600; color:#4ade80;">({{ "{:,.0f}".format(kpi.onay_total_amt or 0) }} TL)</span></div>
            </div>
            <div class="stat-card red">
                <div class="s-label">🚫 Bloke Oldu</div>
                <div class="s-value">{{ kpi.bloke_count }}</div>
            </div>
            <div class="stat-card">
                <div class="s-label">🔴 Olumsuz / Kaçan</div>
                <div class="s-value">{{ kpi.olumsuz_count }}</div>
            </div>
        </div>

        <!-- Günlük Canlı Özet & Hareket Akışı (Mini App Özel) -->
        <div class="today-summary-box" style="margin-bottom:16px; background:var(--card-bg); border:1px solid var(--border-color); border-radius:14px; padding:14px; box-shadow:0 2px 8px rgba(0,0,0,0.15);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; cursor:pointer;" onclick="toggleDailySummary()">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span style="font-size:16px;">⚡</span>
                    <h3 style="margin:0; font-size:14px; font-weight:700; color:var(--text-color);">Günün Özeti & Canlı Hareketler ({{ today_summary.today_date }})</h3>
                    <span style="font-size:11px; background:rgba(56,189,248,0.15); color:#38bdf8; padding:2px 6px; border-radius:10px; font-weight:600;">{{ today_summary.total_actions }} Olay</span>
                </div>
                <span id="daily-summary-icon" style="font-size:12px; color:var(--hint-color);">▼</span>
            </div>

            <div id="daily-summary-body">
                <!-- Hızlı Rozetler -->
                <div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px;">
                    <span style="font-size:11px; padding:4px 8px; border-radius:8px; background:rgba(250,204,21,0.12); color:#facc15; border:1px solid rgba(250,204,21,0.25);">
                        💳 <b>{{ today_summary.kredi_count }} Kredi</b> ({{ "{:,.0f}".format(today_summary.kredi_amt or 0) }} TL)
                    </span>
                    <span style="font-size:11px; padding:4px 8px; border-radius:8px; background:rgba(74,222,128,0.12); color:#4ade80; border:1px solid rgba(74,222,128,0.25);">
                        ✅ <b>{{ today_summary.onay_count }} Onay</b> ({{ "{:,.0f}".format(today_summary.onay_amt or 0) }} TL)
                    </span>
                    <span style="font-size:11px; padding:4px 8px; border-radius:8px; background:rgba(56,189,248,0.12); color:#38bdf8; border:1px solid rgba(56,189,248,0.25);">
                        ↪️ <b>{{ today_summary.forward_count }} Aktarım</b>
                    </span>
                    <span style="font-size:11px; padding:4px 8px; border-radius:8px; background:rgba(239,68,68,0.12); color:#ef4444; border:1px solid rgba(239,68,68,0.25);">
                        🚫 <b>{{ today_summary.bloke_count }} Bloke</b> / 🔴 <b>{{ today_summary.olumsuz_count }} Olumsuz</b>
                    </span>
                </div>

                <!-- Aktif Kullanıcılar -->
                {% if today_summary.top_users %}
                <div style="margin-bottom:10px; font-size:12px; color:var(--text-color); display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                    <span style="color:var(--hint-color); font-weight:600;">👷 Bugün Etkileşim Verenler:</span>
                    {% for uname, cnt in today_summary.top_users %}
                    <span style="background:rgba(255,255,255,0.06); padding:2px 8px; border-radius:6px; border:1px solid var(--border-color); font-size:11px;">
                        <b>{{ uname }}</b> <span style="color:#38bdf8;">({{ cnt }} işlem)</span>
                    </span>
                    {% endfor %}
                </div>
                {% endif %}

                <!-- Canlı Hareket Akışı (Son Olaylar) -->
                <div style="max-height:180px; overflow-y:auto; display:flex; flex-direction:column; gap:4px; padding-right:4px;">
                    {% if today_summary.feed %}
                        {% for ev in today_summary.feed %}
                        <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px; background:rgba(0,0,0,0.15); padding:4px 8px; border-radius:6px; border-left:3px solid {{ ev.type_color }};">
                            <div style="display:flex; align-items:center; gap:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                                <span style="color:var(--hint-color); font-family:monospace;">{{ ev.time }}</span>
                                <span style="font-weight:700; color:{{ ev.type_color }};">{{ ev.type_label }}</span>
                                {% if ev.row_num %}<span style="color:var(--hint-color);">#{{ ev.row_num }}</span>{% endif %}
                                {% if ev.amount %}<span style="color:#facc15; font-weight:600;">({{ "{:,.0f}".format(ev.amount) }} TL)</span>{% endif %}
                                {% if ev.group %}<span style="color:#38bdf8;">➡️ {{ ev.group }}</span>{% endif %}
                                {% if ev.extra %}<span style="color:var(--hint-color); font-style:italic;">({{ ev.extra }})</span>{% endif %}
                            </div>
                            <span style="font-weight:600; color:var(--text-color); margin-left:8px; white-space:nowrap;">{{ ev.user }}</span>
                        </div>
                        {% endfor %}
                    {% else %}
                        <div style="font-size:12px; color:var(--hint-color); font-style:italic; padding:6px 0;">Henüz gün içinde kaydedilen bir hareket yok.</div>
                    {% endif %}
                </div>
            </div>
        </div>

        <!-- Sheet Tabs -->
        <div class="tabs-scroller">
            {% for sheet in sheets %}
            <button class="sheet-tab {% if loop.first %}active{% endif %}" onclick="switchTab('tab-{{ sheet.id }}', this)">
                {{ sheet.name }} ({{ sheet.visible_count }})
            </button>
            {% endfor %}
        </div>

        <!-- Sheet Panels -->
        {% for sheet in sheets %}
        <div id="tab-{{ sheet.id }}" class="tab-panel {% if loop.first %}active{% endif %}">
            <div class="search-container" style="display:flex; gap:8px; align-items:center;">
                <div style="position:relative; flex:1;">
                    <span class="search-icon" style="position:absolute; left:12px; top:50%; transform:translateY(-50%);">🔍</span>
                    <input type="text" class="search-input" style="padding-left:34px;" placeholder="TC, telefon veya durum ara..." oninput="filterCards(this, 'list-{{ sheet.id }}')">
                </div>
                <button type="button" class="f-btn" id="sort-btn-{{ sheet.id }}" onclick="toggleSortOrder('list-{{ sheet.id }}', this)" title="Kayıt Numarasına Göre Sırala">
                    🔢 Yeniden Eskiye (Varsayılan)
                </button>
            </div>

            <div class="cards-list" id="list-{{ sheet.id }}">
                {% if sheet.rows %}
                    {% for row in sheet.rows|reverse %}
                    <div class="data-card" id="card-{{ sheet.id }}-{{ row.num }}" data-search="{{ row.tc_no }} {{ row.phone }} {{ row.calisma_durumu }} {{ row.lead_status }}">
                        <div class="card-top">
                            <div style="display:flex; align-items:center; gap:6px;">
                                <span class="card-id-tag">#{{ row.num }}</span>
                                {% if row.lead_status %}
                                <span style="font-size:10px; font-weight:700; background:rgba(34,197,94,0.15); color:#4ade80; border:1px solid rgba(34,197,94,0.3); padding:1px 6px; border-radius:6px;" title="{{ row.lead_user }} - {{ row.lead_time }}">
                                    {{ row.lead_status }}
                                </span>
                                {% endif %}
                                {% if row.stay_duration %}
                                <span style="font-size:10px; font-weight:700; background:rgba(234,179,8,0.15); color:#facc15; border:1px solid rgba(234,179,8,0.3); padding:1px 6px; border-radius:6px;" title="Aktarılan: {{ row.fwd_group }}">
                                    ⏱️ {{ row.stay_duration }}
                                </span>
                                {% endif %}
                            </div>
                            <span class="card-date">🕒 {{ row.created_time }}</span>
                        </div>
                        <div class="card-body-grid">
                            <div class="field-box">
                                <span class="f-label">Durum</span>
                                <span class="f-val">{{ row.calisma_durumu }}</span>
                            </div>
                            <div class="field-box">
                                <span class="f-label">Kart Limiti</span>
                                <span class="f-val">{{ row.kart_limit }}</span>
                            </div>
                            <div class="field-box full-width">
                                <span class="f-label">T.C. Kimlik</span>
                                <div class="tc-btn" onclick="copyTC(this, '{{ row.tc_no }}')">
                                    <span>{{ row.tc_no }}</span>
                                    <span style="font-size:11px; opacity:0.8;">📋 Kopyala</span>
                                </div>
                            </div>
                            <div class="field-box full-width">
                                <span class="f-label">Telefon Numarası</span>
                                <span class="f-val">{{ row.phone }}</span>
                            </div>
                            {% if row.lead_note %}
                            <div class="field-box full-width" style="background:rgba(234,179,8,0.08); border:1px solid rgba(234,179,8,0.2); border-radius:8px; padding:6px 8px;">
                                <span class="f-label" style="color:#facc15;">📝 Alınan Not</span>
                                <span class="f-val" style="color:#fef08a; white-space:normal;">{{ row.lead_note }}</span>
                            </div>
                            {% endif %}
                        </div>

                        <!-- Card Action Buttons -->
                        <div class="card-actions">
                            <button type="button" class="act-btn copy-all" onclick="copyFullRecord(this, '{{ row.num }}', '{{ row.created_time }}', '{{ row.calisma_durumu }}', '{{ row.tc_no }}', '{{ row.kart_limit }}', '{{ row.phone }}')">
                                <span>📋</span>
                                <span>Tümünü Kopyala</span>
                            </button>
                            <button type="button" class="act-btn delete" onclick="deleteRecordPrompt('{{ sheet.id }}', '{{ row.raw_row_index }}')">
                                <span>🗑️</span>
                                <span>Sil (Chatten de)</span>
                            </button>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div class="empty-box">
                        <div class="e-icon">📭</div>
                        <p>Henüz kayıt bulunamadı</p>
                    </div>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>

    <!-- Links Management Modal -->
    <div class="links-modal" id="linksModal">
        <div class="modal-content">
            <div class="modal-head">
                <h2>🔗 Link & Sheet Yönetimi</h2>
                <button class="close-btn" onclick="closeLinksModal()">✕</button>
            </div>
            <div class="modal-body">
                <!-- Add Form -->
                <div class="add-sheet-box">
                    <h3>+ Yeni Google Sheets Linki Ekle</h3>
                    <input type="text" id="newSheetName" class="form-input" placeholder="Sheet Adı (Örn: 2. Satış Formu)">
                    <input type="url" id="newSheetUrl" class="form-input" placeholder="https://docs.google.com/spreadsheets/d/.../edit">
                    <button type="button" class="submit-btn" onclick="submitAddSheet()">+ Sheet Ekle</button>
                </div>

                <!-- Existing Sheets List -->
                <div style="font-size:12px; font-weight:700; color:var(--hint-color); text-transform:uppercase;">Kayıtlı Linkler</div>
                <div id="sheetsListContainer" style="display:flex; flex-direction:column; gap:8px;">
                    {% for s in all_sheets_raw %}
                    <div class="sheet-item" id="sheet-item-{{ s.id }}">
                        <div class="sheet-item-top">
                            <span class="sheet-name">{{ s.name }}</span>
                            <span class="status-tag {% if s.active %}active{% else %}passive{% endif %}">
                                {% if s.active %}🟢 Aktif{% else %}⚪ Pasif{% endif %}
                            </span>
                        </div>
                        <div class="sheet-meta">
                            Kayıt: <b>{{ s.count }}</b> | Durum: <b>{{ s.status }}</b><br>
                            Son Kontrol: {{ s.last_check }}
                        </div>
                        <div class="sheet-actions">
                            <button class="sm-btn toggle" onclick="toggleSheet('{{ s.id }}')">
                                {% if s.active %}Duraklat (Pasif){% else %}Aktif Et{% endif %}
                            </button>
                            <button class="sm-btn del" onclick="removeSheet('{{ s.id }}', '{{ s.name }}')">
                                🗑️ Sil
                            </button>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>

    <!-- Groups Management Modal -->
    <div class="links-modal" id="groupsModal">
        <div class="modal-content">
            <div class="modal-head">
                <h2>👥 Hedef Grup Yönetimi</h2>
                <button class="close-btn" onclick="closeGroupsModal()">✕</button>
            </div>
            <div class="modal-body">
                <div class="add-sheet-box">
                    <h3>+ Yeni Hedef Grup Ekle</h3>
                    <input type="text" id="newGroupName" class="form-input" placeholder="Grup Adı (Örn: Satış Ekibi 1)">
                    <input type="text" id="newGroupChatId" class="form-input" placeholder="Chat ID (Örn: -1001234567890)">
                    <button type="button" class="submit-btn" onclick="submitAddGroup()">+ Grubu Kaydet</button>
                    <div style="font-size:11px; color:var(--hint-color); margin-top:2px;">
                        💡 İpucu: Bota gruptayken <code>/grup_ekle Grup Adı</code> yazarak da grubu otomatik ekleyebilirsiniz!
                    </div>
                </div>

                <div style="font-size:12px; font-weight:700; color:var(--hint-color); text-transform:uppercase;">Aktarım Yapılabilecek Gruplar</div>
                <div id="groupsListContainer" style="display:flex; flex-direction:column; gap:8px;">
                    {% for g in all_groups %}
                    <div class="sheet-item" id="group-item-{{ g.id }}">
                        <div class="sheet-item-top">
                            <span class="sheet-name">👥 {{ g.name }}</span>
                            <span style="font-family:monospace; font-size:11px; color:#a5b4fc;">{{ g.id }}</span>
                        </div>
                        <div class="sheet-actions">
                            <button class="sm-btn del" onclick="removeGroup('{{ g.id }}', '{{ g.name }}')">
                                🗑️ Grubu Kaldır
                            </button>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>

    <!-- Toast -->
    <div class="toast" id="toast">Bildirim</div>

    <script>
        if (window.Telegram && Telegram.WebApp) {
            Telegram.WebApp.ready();
            Telegram.WebApp.expand();
            Telegram.WebApp.headerColor = Telegram.WebApp.themeParams.bg_color || '#0c0e14';
        }

        function showToast(msg) {
            const toast = document.getElementById("toast");
            toast.innerText = msg;
            toast.classList.add("show");
            setTimeout(() => toast.classList.remove("show"), 2200);
        }

        function triggerHaptic(type = "light") {
            if (window.Telegram && Telegram.WebApp && Telegram.WebApp.HapticFeedback) {
                Telegram.WebApp.HapticFeedback.notificationOccurred(type === "success" ? "success" : "warning");
            }
        }

        function toggleDailySummary() {
            const el = document.getElementById("daily-summary-body");
            const icon = document.getElementById("daily-summary-icon");
            if (!el) return;
            if (el.style.display === "none") {
                el.style.display = "block";
                if (icon) icon.textContent = "▼";
            } else {
                el.style.display = "none";
                if (icon) icon.textContent = "▶";
            }
        }

        function switchTab(panelId, tabEl) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
            const target = document.getElementById(panelId);
            if (target) target.classList.add('active');
            tabEl.classList.add('active');
        }

        function filterCards(input, containerId) {
            const q = input.value.toLowerCase().trim();
            const cards = document.querySelectorAll('#' + containerId + ' .data-card');
            cards.forEach(c => {
                const txt = c.getAttribute('data-search').toLowerCase();
                c.style.display = txt.includes(q) ? '' : 'none';
            });
        }

        function toggleSortOrder(containerId, btn) {
            const container = document.getElementById(containerId);
            if (!container) return;
            const cards = Array.from(container.children);
            cards.reverse();
            cards.forEach(c => container.appendChild(c));

            const isAsc = btn.getAttribute('data-asc') === 'true';
            if (isAsc) {
                btn.setAttribute('data-asc', 'false');
                btn.innerHTML = '🔢 Yeniden Eskiye';
            } else {
                btn.setAttribute('data-asc', 'true');
                btn.innerHTML = '🔢 Eskiden Yeniye';
            }
            triggerHaptic("selection");
        }

        // ── Kopyalama Fonksiyonları ──

        function copyTC(btn, tc) {
            navigator.clipboard.writeText(tc).then(() => {
                btn.classList.add('copied');
                const prev = btn.innerHTML;
                btn.innerHTML = '<span>' + tc + '</span><span style="font-size:11px;">✓ Kopyalandı</span>';
                triggerHaptic("success");
                showToast("T.C. panoya kopyalandı!");
                setTimeout(() => {
                    btn.innerHTML = prev;
                    btn.classList.remove('copied');
                }, 1200);
            });
        }

        function copyFullRecord(btn, num, date, durum, tc, limit, phone) {
            const text = "📋 Kayıt #" + num + "\\n" +
                         "━━━━━━━━━━━━━━━━━━━━━━\\n" +
                         "🕐 Tarih: " + date + "\\n" +
                         "💼 Çalışma Durumu: " + durum + "\\n" +
                         "🆔 T.C. No: " + tc + "\\n" +
                         "💳 Kart Limiti: " + limit + "\\n" +
                         "📞 Telefon: " + phone + "\\n" +
                         "━━━━━━━━━━━━━━━━━━━━━━";

            navigator.clipboard.writeText(text).then(() => {
                btn.classList.add('copied');
                const prev = btn.innerHTML;
                btn.innerHTML = '<span>✓</span><span>Kopyalandı!</span>';
                triggerHaptic("success");
                showToast("Tüm kayıt panoya kopyalandı!");
                setTimeout(() => {
                    btn.innerHTML = prev;
                    btn.classList.remove('copied');
                }, 1500);
            });
        }

        // ── Kayıt Silme Fonksiyonu ──

        function deleteRecordPrompt(sheetId, rowNum) {
            if (!confirm("#" + rowNum + " numaralı kayıt panelden ve Telegram grubundan silinsin mi?")) {
                return;
            }

            fetch("/api/delete-record", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ sheet_id: sheetId, row_num: rowNum })
            })
            .then(res => res.json())
            .then(data => {
                if (data.ok) {
                    const card = document.getElementById("card-" + sheetId + "-" + rowNum);
                    if (card) {
                        card.classList.add("removing");
                        setTimeout(() => card.remove(), 250);
                    }
                    triggerHaptic("success");
                    showToast(data.message || "Kayıt başarıyla silindi!");
                } else {
                    alert("Hata: " + (data.error || "Silinemedi"));
                }
            })
            .catch(err => alert("Bağlantı hatası: " + err));
        }

        // ── Link Yönetimi Modalı ──

        function openLinksModal() {
            document.getElementById("linksModal").classList.add("open");
        }
        function closeLinksModal() {
            document.getElementById("linksModal").classList.remove("open");
        }

        function submitAddSheet() {
            const name = document.getElementById("newSheetName").value.trim();
            const url = document.getElementById("newSheetUrl").value.trim();

            if (!url) {
                alert("Lütfen geçerli bir Google Sheets linki girin.");
                return;
            }

            fetch("/api/add-sheet", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: name, url: url })
            })
            .then(async res => {
                const data = await res.json().catch(() => ({ ok: false, error: "Sunucu hatası veya geçersiz yanıt." }));
                if (res.ok && data.ok) {
                    showToast("Sheet eklendi! Yenileniyor...");
                    triggerHaptic("success");
                    setTimeout(() => location.reload(), 800);
                } else {
                    alert("Uyarı: " + (data.error || data.message || "Link eklenemedi."));
                }
            })
            .catch(err => alert("Bağlantı hatası: " + err));
        }

        function toggleSheet(sheetId) {
            fetch("/api/toggle-sheet", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ sheet_id: sheetId })
            })
            .then(res => res.json())
            .then(data => {
                if (data.ok) {
                    showToast("Sheet durumu güncellendi!");
                    setTimeout(() => location.reload(), 600);
                } else {
                    alert("Hata: " + data.error);
                }
            });
        }

        function removeSheet(sheetId, name) {
            if (!confirm("'" + name + "' sheet linkini silmek istediğinize emin misiniz?")) {
                return;
            }

            fetch("/api/delete-sheet", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ sheet_id: sheetId })
            })
            .then(res => res.json())
            .then(data => {
                if (data.ok) {
                    showToast("Sheet silindi!");
                    setTimeout(() => location.reload(), 600);
                } else {
                    alert("Hata: " + data.error);
                }
            });
        }

        // ── Grup Yönetimi Modalı ──

        function openGroupsModal() {
            document.getElementById("groupsModal").classList.add("open");
        }
        function closeGroupsModal() {
            document.getElementById("groupsModal").classList.remove("open");
        }

        function submitAddGroup() {
            const name = document.getElementById("newGroupName").value.trim();
            const chatId = document.getElementById("newGroupChatId").value.trim();

            if (!chatId) {
                alert("Lütfen geçerli bir Grup Chat ID girin.");
                return;
            }

            fetch("/api/add-group", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: name, chat_id: chatId })
            })
            .then(async res => {
                const data = await res.json().catch(() => ({ ok: false, error: "Sunucu hatası" }));
                if (res.ok && data.ok) {
                    showToast("Grup eklendi! Yenileniyor...");
                    triggerHaptic("success");
                    setTimeout(() => location.reload(), 800);
                } else {
                    alert("Hata: " + (data.error || data.message || "Grup eklenemedi."));
                }
            })
            .catch(err => alert("Bağlantı hatası: " + err));
        }

        function removeGroup(chatId, name) {
            if (!confirm("'" + name + "' grubunu aktarım listesinden kaldırmak istediğinize emin misiniz?")) {
                return;
            }

            fetch("/api/delete-group", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ chat_id: chatId })
            })
            .then(res => res.json())
            .then(data => {
                if (data.ok) {
                    showToast("Grup kaldırıldı!");
                    setTimeout(() => location.reload(), 600);
                } else {
                    alert("Hata: " + data.error);
                }
            });
        }
    </script>
</body>
</html>
"""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = get_client_ip()
    allowed, remaining, wait_sec = check_rate_limit(ip)

    if not allowed:
        return render_template_string(
            LOGIN_HTML,
            locked=True,
            wait_sec=wait_sec,
            error=None,
            remaining=0,
        )

    error = None
    if request.method == "POST":
        pin = request.form.get("pin", "")
        if pin == ADMIN_PIN:
            record_successful_login(ip)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            rem, wait = record_failed_attempt(ip)
            if rem == 0:
                return render_template_string(
                    LOGIN_HTML,
                    locked=True,
                    wait_sec=wait,
                    error=None,
                    remaining=0,
                )
            error = "Yanlış PIN kodu!"
            remaining = rem

    return render_template_string(
        LOGIN_HTML,
        locked=False,
        wait_sec=0,
        error=error,
        remaining=remaining,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    filter_mode = request.args.get("filter", "today")
    now_dt = datetime.now()
    start_date = None
    end_date = None

    if filter_mode == "today":
        start_date = end_date = now_dt.strftime("%Y-%m-%d")
    elif filter_mode == "yesterday":
        yest = now_dt.fromtimestamp(now_dt.timestamp() - 86400)
        start_date = end_date = yest.strftime("%Y-%m-%d")
    elif filter_mode == "week":
        week_ago = now_dt.fromtimestamp(now_dt.timestamp() - (7 * 86400))
        start_date = week_ago.strftime("%Y-%m-%d")
        end_date = now_dt.strftime("%Y-%m-%d")
    elif filter_mode == "month":
        month_ago = now_dt.fromtimestamp(now_dt.timestamp() - (30 * 86400))
        start_date = month_ago.strftime("%Y-%m-%d")
        end_date = now_dt.strftime("%Y-%m-%d")
    elif filter_mode == "all":
        start_date = "2020-01-01"
        end_date = "2099-12-31"

    kpi = get_dashboard_metrics(start_date, end_date)

    all_sheets_raw = get_sheets()
    sheets_data = []
    total_rows = 0
    active_count = 0

    for sheet_config in all_sheets_raw:
        sheet_id = sheet_config.get("id")
        if not sheet_id:
            try:
                sheet_id = extract_sheet_id(sheet_config["url"])
            except Exception:
                continue

        if sheet_config.get("active", True):
            active_count += 1

        sheet_info = {
            "id": sheet_id,
            "name": sheet_config.get("name", "Form"),
            "url": sheet_config.get("url", ""),
            "active": sheet_config.get("active", True),
            "status": sheet_config.get("status", "Aktif"),
            "last_check": sheet_config.get("last_check", "—"),
            "count": 0,
            "visible_count": 0,
            "rows": []
        }

        try:
            raw_rows = fetch_sheet_data(sheet_id)
            if raw_rows:
                headers = list(raw_rows[0].keys())
                col_mapping = find_column_mapping(headers)

                for i, row in enumerate(raw_rows):
                    # Panelden silinmiş kayıtları gösterme
                    if is_record_deleted(sheet_id, i):
                        continue

                    created_raw = row.get(col_mapping.get("created_time", ""), "")
                    st_info = get_record_status(sheet_id, i)
                    fwd_ev = get_forward_event(sheet_id, i)
                    fwd_group = fwd_ev.get("target_chat_name", "") if fwd_ev else ""
                    stay_dur = ""
                    if fwd_ev:
                        diff = max(0, time.time() - fwd_ev.get("fwd_timestamp", time.time()))
                        stay_dur = format_duration(diff)

                    global_id = get_record_global_id(sheet_id, i)

                    raw_phone = str(row.get(col_mapping.get("phone_number", ""), "") or "").strip()
                    if raw_phone.startswith("p:"):
                        raw_phone = raw_phone[2:]

                    sheet_info["rows"].append({
                        "num": global_id,
                        "raw_row_index": i,
                        "created_time": convert_to_turkey_time(created_raw),
                        "calisma_durumu": row.get(col_mapping.get("çalışma_durumu", ""), "—"),
                        "tc_no": row.get(col_mapping.get("t.c_numaranız", ""), "—"),
                        "kart_limit": row.get(col_mapping.get("kullanılabilir_kart_limitiniz", ""), "—"),
                        "phone": raw_phone or "—",
                        "lead_status": st_info.get("status", ""),
                        "lead_note": st_info.get("note", ""),
                        "lead_user": st_info.get("user", ""),
                        "lead_time": st_info.get("time", ""),
                        "fwd_group": fwd_group,
                        "stay_duration": stay_dur,
                    })

                sheet_info["count"] = len(raw_rows)
                sheet_info["visible_count"] = len(sheet_info["rows"])
                total_rows += sheet_info["visible_count"]

        except Exception as e:
            logger.error(f"Sheet verisi çekilemedi ({sheet_info['name']}): {e}")
            sheet_info["status"] = "Hata"

        sheets_data.append(sheet_info)

    all_groups = get_groups()
    today_summary = get_today_summary()
    all_users = get_all_users()

    return render_template_string(
        DASHBOARD_HTML,
        sheets=sheets_data,
        all_sheets_raw=all_sheets_raw,
        total_sheets=len(all_sheets_raw),
        active_sheets_count=active_count,
        total_rows=total_rows,
        all_groups=all_groups,
        kpi=kpi,
        active_filter=filter_mode,
        today_summary=today_summary,
        all_users=all_users,
    )


# ── REST API Endpoints ───────────────────────────────────────────────────────

@app.route("/api/add-sheet", methods=["POST"])
@login_required
def api_add_sheet():
    data = request.get_json() or {}
    name = data.get("name", "")
    url = data.get("url", "")
    main_chat = get_main_chat_id()
    success, msg = add_sheet(name, url, chat_id=main_chat)
    if success:
        # Yeni eklenen sheet'i hemen tara ve bekleyen satırları gruba at!
        try:
            from bot import check_and_send_sheet, extract_sheet_id
            sheet_id = extract_sheet_id(url)
            threading.Thread(target=check_and_send_sheet, args=({"name": name or "Form", "url": url, "id": sheet_id, "chat_id": main_chat}, True), daemon=True).start()
        except Exception as e:
            logger.error(f"Anlık sheet çekme hatası: {e}")
        return jsonify({"ok": True, "message": msg})
    return jsonify({"ok": False, "error": msg}), 400


@app.route("/api/delete-sheet", methods=["POST"])
@login_required
def api_delete_sheet():
    data = request.get_json() or {}
    sheet_id = data.get("sheet_id", "")
    if delete_sheet(sheet_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Sheet silinemedi."}), 400


@app.route("/api/toggle-sheet", methods=["POST"])
@login_required
def api_toggle_sheet():
    data = request.get_json() or {}
    sheet_id = data.get("sheet_id", "")
    if toggle_sheet_active(sheet_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Durum değiştirilemedi."}), 400


@app.route("/api/delete-record", methods=["POST"])
@login_required
def api_delete_record():
    data = request.get_json() or {}
    sheet_id = data.get("sheet_id", "")
    row_num = data.get("row_num")

    if not sheet_id or row_num is None:
        return jsonify({"ok": False, "error": "Geçersiz parametreler."}), 400

    try:
        success, msg = delete_record(sheet_id, int(row_num))
        return jsonify({"ok": success, "message": msg})
    except Exception as e:
        logger.error(f"Kayıt silme hatası: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/add-group", methods=["POST"])
@login_required
def api_add_group():
    data = request.get_json() or {}
    name = data.get("name", "")
    chat_id = data.get("chat_id", "")
    success, msg = add_group(name, chat_id)
    if success:
        return jsonify({"ok": True, "message": msg})
    return jsonify({"ok": False, "error": msg}), 400


@app.route("/api/delete-group", methods=["POST"])
@login_required
def api_delete_group():
    data = request.get_json() or {}
    chat_id = data.get("chat_id", "")
    if delete_group(chat_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Grup silinemedi."}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
