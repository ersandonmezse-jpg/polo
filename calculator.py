"""
Matematik & Kripto/Döviz Çevirici Modülü
========================================
- 100 try to trx, 100 usdt to trx gibi kripto & döviz çevirileri (Canlı Binance API)
- 100 try %3, 1000 %15 gibi yüzdelik hesaplamaları
- Basit matematiksel işlem çözücü (5000 * 0.15, 12500 / 4 vb.)
"""

import re
import html
import time
import requests
import logging
from typing import Optional, Tuple, Dict

logger = logging.getLogger(__name__)

# Fiyat önbelleği (30 saniye TTL - API limitlerini korur, anında yanıt verir)
_PRICE_CACHE: Dict[str, Tuple[float, float]] = {}
_PRICE_CACHE_TTL = 30.0

# Para birimi eşleştirme tablosu
CURRENCY_ALIASES = {
    "try": "TRY",
    "tl": "TRY",
    "tl.": "TRY",
    "türk lirası": "TRY",
    "turk lirasi": "TRY",
    "usdt": "USDT",
    "tether": "USDT",
    "usd": "USDT",
    "dolar": "USDT",
    "$": "USDT",
    "trx": "TRX",
    "tron": "TRX",
    "btc": "BTC",
    "bitcoin": "BTC",
    "eth": "ETH",
    "ethereum": "ETH",
    "sol": "SOL",
    "solana": "SOL",
    "bnb": "BNB",
    "eur": "EUR",
    "euro": "EUR",
    "€": "EUR",
}


def normalize_currency(raw: str) -> Optional[str]:
    """Para birimi takma adlarını standart sembole dönüştürür."""
    if not raw:
        return None
    c = raw.strip().lower()
    return CURRENCY_ALIASES.get(c, c.upper() if len(c) in (3, 4) else None)


def get_binance_price(symbol: str) -> Optional[float]:
    """Binance API'den sembol fiyatını çeker (30s önbellekli)."""
    clean_sym = symbol.upper().strip()
    now = time.time()

    if clean_sym in _PRICE_CACHE:
        price, ts = _PRICE_CACHE[clean_sym]
        if now - ts < _PRICE_CACHE_TTL:
            return price

    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={clean_sym}"
        res = requests.get(url, timeout=4)
        if res.status_code == 200:
            val = float(res.json().get("price", 0))
            if val > 0:
                _PRICE_CACHE[clean_sym] = (val, now)
                return val
    except Exception as e:
        logger.debug(f"Binance fiyat çekme hatası ({clean_sym}): {e}")

    return None


def get_exchange_rate(from_curr: str, to_curr: str) -> Optional[Tuple[float, str]]:
    """İki para birimi arasındaki dönüşüm kurunu (1 from_curr = ? to_curr) ve kaynak bilgiyi hesaplar."""
    f = normalize_currency(from_curr)
    t = normalize_currency(to_curr)

    if not f or not t:
        return None
    if f == t:
        return 1.0, f"1 {f} = 1 {t}"

    # 1. Doğrudan sembol dene (örn: TRXTRY, USDTTRY, TRXUSDT)
    direct_sym = f"{t}{f}"
    p_direct = get_binance_price(direct_sym)
    if p_direct:
        # direct_sym = t/f -> 1 t = p_direct f -> 1 f = (1 / p_direct) t
        rate = 1.0 / p_direct
        return rate, f"1 {t} = {p_direct:g} {f}"

    inv_sym = f"{f}{t}"
    p_inv = get_binance_price(inv_sym)
    if p_inv:
        # inv_sym = f/t -> 1 f = p_inv t
        rate = p_inv
        return rate, f"1 {f} = {p_inv:g} {t}"

    # 2. İkili dönüşüm (USDT üzerinden çapraz kur: f -> USDT -> t)
    # from_curr -> USDT
    rate_f_usdt = None
    if f == "USDT":
        rate_f_usdt = 1.0
    else:
        p1 = get_binance_price(f"{f}USDT")
        if p1:
            rate_f_usdt = p1
        else:
            p1_inv = get_binance_price(f"USDT{f}")
            if p1_inv:
                rate_f_usdt = 1.0 / p1_inv

    # to_curr -> USDT
    rate_t_usdt = None
    if t == "USDT":
        rate_t_usdt = 1.0
    else:
        p2 = get_binance_price(f"{t}USDT")
        if p2:
            rate_t_usdt = p2
        else:
            p2_inv = get_binance_price(f"USDT{t}")
            if p2_inv:
                rate_t_usdt = 1.0 / p2_inv

    if rate_f_usdt and rate_t_usdt:
        final_rate = rate_f_usdt / rate_t_usdt
        return final_rate, f"Çapraz Kur (1 {f} = {final_rate:.4f} {t})"

    return None


def calculate_currency_conversion(amount: float, from_curr: str, to_curr: str) -> Optional[str]:
    """Döviz / Kripto dönüşümünü hesaplayıp şık Telegram HTML kartı döner."""
    rate_info = get_exchange_rate(from_curr, to_curr)
    if not rate_info:
        return None

    rate, rate_note = rate_info
    converted_amount = amount * rate
    f_sym = normalize_currency(from_curr)
    t_sym = normalize_currency(to_curr)

    # Ondalık hassasiyetini ayarla
    if converted_amount >= 100:
        res_str = f"{converted_amount:,.2f}"
    elif converted_amount >= 1:
        res_str = f"{converted_amount:.4f}"
    else:
        res_str = f"{converted_amount:.6f}"

    card = (
        f"💱 <b>DÖVİZ & KRİPTO ÇEVİRİCİ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Verilen Tutar:</b> {amount:,.2f} {f_sym}\n"
        f"🎯 <b>Karşılığı:</b> <b>{res_str} {t_sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Kur Bilgisi:</b> {rate_note}\n"
        f"⏱️ <i>Canlı Binance Verisi</i>"
    )
    return card


def calculate_percentage(amount: float, percent: float, currency_unit: str = "") -> str:
    """Yüzdelik hesaplamayı yapar (İskonto/Komisyon kesintisi ve ekleme sonuçlarıyla)."""
    percent_val = amount * (percent / 100.0)
    net_minus = amount - percent_val
    net_plus = amount + percent_val

    unit_str = f" {currency_unit.upper()}" if currency_unit else " TL"

    card = (
        f"📐 <b>YÜZDE HESAPLAMA (%{percent:g})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Ana Tutar:</b> {amount:,.2f}{unit_str}\n"
        f"📊 <b>Yüzdelik (%{percent:g}):</b> <b>{percent_val:,.2f}{unit_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"➖ <b>Kesinti Sonrası (Kalan):</b> <code>{net_minus:,.2f}{unit_str}</code>\n"
        f"➕ <b>Ekleme Sonrası (Toplam):</b> <code>{net_plus:,.2f}{unit_str}</code>"
    )
    return card


def calculate_math_expression(expression: str) -> Optional[str]:
    """Güvenli basit matematik işlemi (toplama, çıkarma, çarpma, bölme) çözer."""
    clean_expr = expression.replace("x", "*").replace("X", "*").replace(",", ".").replace(" ", "")
    # Sadece sayılar ve temel aritmetik işaretlerine izin ver
    if not re.match(r"^[\d\.\+\-\*\/\(\)]+$", clean_expr):
        return None

    # Güvenli değerlendirme
    try:
        # Sıfıra bölme ve eval güvenliği kontrolü
        result = eval(clean_expr, {"__builtins__": None}, {})
        if isinstance(result, (int, float)):
            if result == int(result):
                res_str = f"{int(result):,}"
            else:
                res_str = f"{result:,.2f}"
            return f"🔢 <b>HESAPLAMA SONUCU:</b>\n\n<code>{expression.strip()} = {res_str}</code>"
    except Exception:
        return None
    return None


def parse_and_process_math_query(text: str) -> Optional[str]:
    """
    Kullanıcının yazdığı mesajı inceler; döviz çevirisi veya yüzde hesabı ise yanıt üretir.
    Örnekler:
    - 100 try to trx
    - 100 usdt to trx
    - 500 trx to try
    - 100 try %3
    - 1000 %15
    - %10 5000
    - /hesap 100 * 50
    """
    if not text:
        return None

    t = text.strip()

    # /hesap veya /cevir veya /yuzde komutu varsa ön eki temizle
    for cmd_prefix in ["/hesap", "/cevir", "/doviz", "/kripto", "/yuzde"]:
        if t.lower().startswith(cmd_prefix):
            t = t[len(cmd_prefix):].strip()
            break

    # 1. Döviz / Kripto Çeviri Kalıbı: "100 try to trx", "50 usdt -> trx", "100 usd trx"
    conv_pattern = r"^(\d+[\d\.,]*)\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\$€]+)\s*(?:to|->|kaç|kac|\/|\s)\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\$€]+)$"
    m_conv = re.match(conv_pattern, t, re.IGNORECASE)
    if m_conv:
        amt_raw, from_curr, to_curr = m_conv.groups()
        amt_clean = float(amt_raw.replace(".", "").replace(",", ".")) if ("," in amt_raw and "." in amt_raw) else float(amt_raw.replace(",", "."))
        res = calculate_currency_conversion(amt_clean, from_curr, to_curr)
        if res:
            return res

    # 2. Yüzde Hesaplama Kalıbı (Standart: "100 try %3", "100 %3", "1000 tl % 15")
    pct_pattern1 = r"^(\d+[\d\.,]*)\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\$€]*)\s*%\s*(\d+[\d\.,]*)$"
    m_pct1 = re.match(pct_pattern1, t, re.IGNORECASE)
    if m_pct1:
        amt_raw, unit, pct_raw = m_pct1.groups()
        amt = float(amt_raw.replace(".", "").replace(",", ".")) if ("," in amt_raw and "." in amt_raw) else float(amt_raw.replace(",", "."))
        pct = float(pct_raw.replace(",", "."))
        return calculate_percentage(amt, pct, unit.strip())

    # Yüzde Ters Kalıp: "%15 1000 tl"
    pct_pattern2 = r"^%\s*(\d+[\d\.,]*)\s*(\d+[\d\.,]*)\s*([a-zA-ZçğıöşüÇĞİÖŞÜ\$€]*)$"
    m_pct2 = re.match(pct_pattern2, t, re.IGNORECASE)
    if m_pct2:
        pct_raw, amt_raw, unit = m_pct2.groups()
        amt = float(amt_raw.replace(".", "").replace(",", ".")) if ("," in amt_raw and "." in amt_raw) else float(amt_raw.replace(",", "."))
        pct = float(pct_raw.replace(",", "."))
        return calculate_percentage(amt, pct, unit.strip())

    # 3. Genel Aritmetik İşlem: "1000 * 0.15", "5000 / 4 + 100"
    if any(op in t for op in ["*", "/", "+", "-", "x", "X"]):
        # Telefon numarası veya tarihle karışmaması için kontrol
        if not re.match(r"^0?5\d{9}$", t) and not re.match(r"^\d{1,2}[\/\.]\d{1,2}[\/\.]\d{2,4}$", t):
            res_math = calculate_math_expression(t)
            if res_math:
                return res_math

    return None
