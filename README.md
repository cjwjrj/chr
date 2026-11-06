# Telegram Video Search Bot

بوت تيليجرام يبحث عن صفحات الفيديو عبر Brave Video Search ويعرض حتى 10 نتائج.
النتائج تفتح في موقع المصدر؛ هذا المشروع لا ينزّل ملفات الفيديو ولا يتجاوز
قيود المواقع أو أنظمة تسجيل الدخول أو حقوق النشر.

## 1) المتطلبات
- Python 3.10 أو أحدث
- توكن بوت من Telegram عبر @BotFather
- مفتاح Brave Search API مع تفعيل/إتاحة Video Search

## 2) التثبيت
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

ضع `BOT_TOKEN` و `BRAVE_API_KEY` داخل `.env`.

## 3) التشغيل
```bash
python main.py
```

## 4) تشغيل دائم عبر systemd
أنشئ `/etc/systemd/system/video-search-bot.service` وعدّل المسارات واسم المستخدم:

```ini
[Unit]
Description=Telegram Video Search Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/opt/telegram_video_search_bot
EnvironmentFile=/opt/telegram_video_search_bot/.env
ExecStart=/opt/telegram_video_search_bot/.venv/bin/python /opt/telegram_video_search_bot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

ثم:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-search-bot
sudo journalctl -u video-search-bot -f
```

## ملاحظات مهمة
- المستخدم يؤكد أن عمره 18 سنة أو أكثر قبل البحث؛ هذا إقرار ذاتي وليس تحققاً من الهوية.
- نتائج البحث قد تختلف حسب بلد المستخدم وتغطية محرك البحث.
- لا يوجد ضمان أن كل نتيجة تحتوي ملف فيديو قابل للتنزيل.
- إرسال ملف الفيديو نفسه يتطلب رابط ملف مباشر أو API من ناشر يصرّح صراحةً
  بالتنزيل وإعادة الإرسال. لا تستخدمه لتجاوز قيود المواقع أو إعادة توزيع
  محتوى محمي بحقوق النشر أو غير رضائي.
- إذا أردت إضافة مصدر مرخّص لاحقاً، اربطه كموفّر بحث منفصل يعيد فقط روابط
  الوسائط المصرّح بها وبيانات الترخيص.
