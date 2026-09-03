# 📋 Google Sheets → Telegram Bot

## Proje Yapısı

| Dosya | Açıklama |
|---|---|
| `bot.py` | Ana bot kodu |
| `config.py` | Ayar dosyası (token, chat ID, sheets URL) |
| `requirements.txt` | Python bağımlılıkları |
| `last_sent_index.txt` | Otomatik oluşur - son gönderilen satır takibi |

## Nasıl Çalışır?

1. Google Sheets'ten CSV olarak veri çeker
2. Şu kolonları filtreler: `created_time`, `çalışma_durumu`, `t.c_numaranız`, `kullanılabilir_kart_limitiniz`, `phone_number`
3. Her satıra **0'dan başlayan** numara atar
4. `created_time` değerini **Türkiye saatine** çevirir
5. Telegram grubuna formatlı mesaj gönderir
6. **20 dakikada bir** yeni verileri kontrol eder
7. Sadece daha önce gönderilmemiş yeni satırları gönderir

## Kurulum

### 1. Telegram Bot Token Alma
1. Telegram'da [@BotFather](https://t.me/BotFather) ile konuşun
2. `/newbot` komutu gönderin
3. Bot adı ve kullanıcı adı belirleyin
4. Verilen **token**'ı kopyalayın

### 2. Grup Chat ID Alma
1. Botu gruba ekleyin
2. Gruba herhangi bir mesaj gönderin
3. Tarayıcıda şu adresi açın:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. JSON çıktısında `"chat":{"id":-100XXXXXXXXXX}` kısmındaki negatif sayıyı kopyalayın

### 3. Google Sheets Hazırlama
1. Sheets'inizin şu kolonları içerdiğinden emin olun:
   - `created_time`
   - `çalışma_durumu`
   - `t.c_numaranız`
   - `kullanılabilir_kart_limitiniz`
   - `phone_number`
2. **Paylaş** → **Bağlantıya sahip olan herkes** → **Görüntüleyici** olarak ayarlayın
3. Sheets URL'sini kopyalayın

### 4. Ayarları Girin
`config.py` dosyasını açın ve bilgilerinizi girin:
```python
TELEGRAM_BOT_TOKEN = "123456:ABC-DEF..."
TELEGRAM_CHAT_ID = "-1001234567890"
GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/XXXXX/edit"
```

### 5. Botu Çalıştırın
```bash
python bot.py
```

## Mesaj Formatı Örneği

```
📋 Kayıt #0
━━━━━━━━━━━━━━━━━━━━━━
🕐 Tarih: 02/09/2026 22:15:30
💼 Çalışma Durumu: Çalışıyor
🆔 T.C. Numarası: 12345678901
💳 Kart Limiti: 5000
📞 Telefon: 05551234567
━━━━━━━━━━━━━━━━━━━━━━
```

## Sıfırlama
Tüm verileri baştan göndermek isterseniz `last_sent_index.txt` dosyasını silin.
