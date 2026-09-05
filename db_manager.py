"""
VPS / MySQL Veritabanı Yöneticisi (tcpro)
=========================================
- tcpro tablosunda TC veya GSM ile hızlı arama
- Uzak sunucu (VPS) bağlantı kontrolü
- Şık Telegram HTML formatlama
"""

import logging
import re
import html
from typing import Optional, Dict, Any, Tuple

from config import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASSWORD,
    DB_NAME,
)

logger = logging.getLogger(__name__)


def is_db_configured() -> bool:
    """Veritabanı bağlantı bilgilerinin girilip girilmediğini kontrol eder."""
    return bool(DB_HOST and DB_USER and DB_NAME)


def get_connection(timeout: int = 6):
    """MySQL / MariaDB bağlantısı oluşturur."""
    if not is_db_configured():
        raise ValueError("DB_HOST veya DB bilgileri yapılandırılmamış.")

    import pymysql

    # Önce standart utf8mb4 ile dene, Türkçe karakter sorunu çıkarsa latin5'e geçer
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=timeout,
            read_timeout=15,
            write_timeout=15,
        )
        return conn
    except Exception:
        # Fallback latin5
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="latin5",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=timeout,
            read_timeout=15,
            write_timeout=15,
        )


def test_db_connection() -> Tuple[bool, str]:
    """Sunucuya ve tcpro tablosuna erişimi test eder."""
    if not is_db_configured():
        return False, "Veritabanı bilgileri tanımlanmamış. Lütfen config veya ortam değişkenlerini (DB_HOST vb.) girin."

    try:
        conn = get_connection(timeout=5)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok;")
            cursor.fetchone()
            # Tablo varlığını ve kayıt sayısını hafifçe kontrol et
            try:
                cursor.execute("SELECT COUNT(*) AS total FROM tcpro;")
                row = cursor.fetchone()
                count = row.get("total", 0) if row else 0
                return True, f"Bağlantı başarılı! 'tcpro' tablosunda toplam {count:,} kayıt bulundu."
            except Exception as te:
                return True, f"Bağlantı kuruldu fakat 'tcpro' tablosu sorgulanamadı: {te}"
        conn.close()
    except Exception as e:
        logger.error(f"DB bağlantı hatası: {e}")
        return False, f"Bağlantı hatası: {str(e)}"


def search_by_tc(tc_raw: str) -> Optional[Dict[str, Any]]:
    """T.C. Kimlik Numarası ile tcpro tablosunda arama yapar."""
    if not tc_raw:
        return None

    clean_tc = re.sub(r"\D", "", str(tc_raw)).strip()
    if not clean_tc:
        return None

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            # Indexli TC alanı üzerinden sorgu (LIMIT 1)
            sql = "SELECT * FROM tcpro WHERE TC = %s LIMIT 1;"
            cursor.execute(sql, (clean_tc,))
            result = cursor.fetchone()
            return result
    except Exception as e:
        logger.error(f"search_by_tc hatası ({clean_tc}): {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def search_by_gsm(phone_raw: str) -> Optional[Dict[str, Any]]:
    """Telefon / GSM ile tcpro tablosunda arama yapar."""
    if not phone_raw:
        return None

    digits = re.sub(r"\D", "", str(phone_raw))
    if not digits:
        return None

    # Standart 10 haneli format (örn: 5321234567)
    if digits.startswith("90") and len(digits) == 12:
        digits10 = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits10 = digits[1:]
    else:
        digits10 = digits

    candidates = list(dict.fromkeys([digits10, f"0{digits10}", f"90{digits10}", digits]))

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            for cand in candidates:
                sql = "SELECT * FROM tcpro WHERE GSM = %s LIMIT 1;"
                cursor.execute(sql, (cand,))
                row = cursor.fetchone()
                if row:
                    return row
        return None
    except Exception as e:
        logger.error(f"search_by_gsm hatası ({phone_raw}): {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def format_tc_card(record: Dict[str, Any]) -> str:
    """Veritabanından dönen kaydı Telegram HTML mesaj formatına dönüştürür."""
    if not record:
        return "❌ Kayıt bulunamadı."

    def val(k: str, default: str = "—") -> str:
        v = record.get(k)
        if v is None or str(v).strip() in ("", "None", "NULL", "0000-00-00"):
            return default
        return html.escape(str(v).strip())

    tc = val("TC")
    ad = val("AD")
    soyad = val("SOYAD")
    cinsiyet = val("CINSIYET")
    medeni = val("MEDENIHAL")
    dogum_tarihi = val("DOGUMTARIHI")
    dogum_yeri = val("DOGUMYERI")
    baba_adi = val("BABAADI")
    baba_tc = val("BABATC")
    anne_adi = val("ANNEADI")
    anne_tc = val("ANNETC")
    adres_il = val("ADRESIL")
    adres_ilce = val("ADRESILCE")
    memleket_il = val("MEMLEKETIL")
    memleket_ilce = val("MEMLEKETILCE")
    memleket_koy = val("MEMLEKETKOY")
    gsm = val("GSM")

    text = (
        f"👤 <b>KİŞİ SORGUSU (TC: <code>{tc}</code>)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Ad Soyad:</b> <b>{ad} {soyad}</b>\n"
        f"🚻 <b>Cinsiyet / Durum:</b> {cinsiyet} | {medeni}\n"
        f"🎂 <b>Doğum Tarihi:</b> {dogum_tarihi} ({dogum_yeri})\n"
        f"👨 <b>Baba:</b> {baba_adi} " + (f"(<code>{baba_tc}</code>)" if baba_tc != "—" else "") + "\n"
        f"👩 <b>Anne:</b> {anne_adi} " + (f"(<code>{anne_tc}</code>)" if anne_tc != "—" else "") + "\n"
        f"📍 <b>İkamet:</b> {adres_il} / {adres_ilce}\n"
        f"🏡 <b>Memleket:</b> {memleket_il} / {memleket_ilce}" + (f" ({memleket_koy})" if memleket_koy != "—" else "") + "\n"
    )
    if gsm != "—":
        text += f"📞 <b>GSM:</b> <code>{gsm}</code>\n"
    text += f"━━━━━━━━━━━━━━━━━━━━━━"

    return text


def enrich_lead(tc_raw: str = "", phone_raw: str = "") -> Optional[Dict[str, Any]]:
    """Yeni gelen başvuru için önce TC ile, bulunamazsa GSM ile veritabanından kişi kaydını çeker."""
    if not is_db_configured():
        return None

    # 1. Önce TC ile ara
    if tc_raw and tc_raw != "—":
        person = search_by_tc(tc_raw)
        if person:
            return person

    # 2. TC yoksa veya bulunamadıysa telefon ile ara
    if phone_raw and phone_raw != "—":
        person = search_by_gsm(phone_raw)
        if person:
            return person

    return None


def format_lead_enrichment_html(record: Optional[Dict[str, Any]]) -> str:
    """Başvuru bildirim kartının içerisine gömülecek zenginleştirilmiş MERNİS bilgilerini üretir."""
    if not record:
        return ""

    def val(k: str, default: str = "—") -> str:
        v = record.get(k)
        if v is None or str(v).strip() in ("", "None", "NULL", "0000-00-00"):
            return default
        return html.escape(str(v).strip())

    ad = val("AD")
    soyad = val("SOYAD")
    baba = val("BABAADI")
    baba_tc = val("BABATC")
    anne = val("ANNEADI")
    anne_tc = val("ANNETC")
    dt = val("DOGUMTARIHI")
    dy = val("DOGUMYERI")
    cinsiyet = val("CINSIYET")
    medeni = val("MEDENIHAL")
    ikamet_il = val("ADRESIL")
    ikamet_ilce = val("ADRESILCE")
    mem_il = val("MEMLEKETIL")
    mem_ilce = val("MEMLEKETILCE")
    mem_koy = val("MEMLEKETKOY")

    lines = ["\n━━━━━━━━━━━━━━━━━━━━━━", "🔍 <b>MERNİS / KİŞİ DOĞRULAMA (Otomatik):</b>"]

    if ad != "—" or soyad != "—":
        lines.append(f"👤 <b>Ad Soyad:</b> <b>{ad} {soyad}</b>")

    dt_str = dt if dt != "—" else ""
    dy_str = f" ({dy})" if dy != "—" else ""
    cins_str = f" | {cinsiyet}" if cinsiyet != "—" else ""
    med_str = f" | {medeni}" if medeni != "—" else ""
    if dt_str or dy_str or cins_str or med_str:
        lines.append(f"🎂 <b>Doğum / Durum:</b> {dt_str}{dy_str}{cins_str}{med_str}")

    baba_part = baba if baba != "—" else ""
    if baba_tc != "—":
        baba_part += f" (TC: <code>{baba_tc}</code>)"
    anne_part = anne if anne != "—" else ""
    if anne_tc != "—":
        anne_part += f" (TC: <code>{anne_tc}</code>)"

    if baba_part or anne_part:
        e_baba = baba_part or "—"
        e_anne = anne_part or "—"
        lines.append(f"👪 <b>Anne / Baba:</b> {e_anne} / {e_baba}")

    if ikamet_il != "—" or ikamet_ilce != "—":
        lines.append(f"📍 <b>İkamet:</b> {ikamet_il} / {ikamet_ilce}")

    if mem_il != "—" or mem_ilce != "—":
        mem_koy_str = f" ({mem_koy})" if mem_koy != "—" else ""
        lines.append(f"🏡 <b>Memleket:</b> {mem_il} / {mem_ilce}{mem_koy_str}")

    return "\n".join(lines)
