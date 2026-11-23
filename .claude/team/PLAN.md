# طرح ربات احراز هویت دانشجویان (تأییدشده توسط کاربر، ۲۰۲۶-۱۱-۲۳؛ هماهنگ با کد 0.1.0)

تصمیم‌ها و دلیل‌ها در `DECISIONS.md` و کارهای باز در `BACKLOG.md` هستند.

## پشته
Python 3.13، python-telegram-bot 22.8 (با extra `job-queue`)، sqlite3 استاندارد با WAL، openpyxl، python-dotenv، tzdata، pytest. نسخه‌ها در `requirements.txt` و `requirements-dev.txt` پین شده‌اند. کد خالص بدون شبکه تست می‌شود. `concurrent_updates` خاموش است (یک اتصال SQLite).

## ساختار
```
bot/
  main.py (polling، لاگ، بکاپ اولیه)، config.py (.env: BOT_TOKEN, ADMIN_IDS, DB_PATH, GROUP_ID, DEFAULT_CONTACT)
  db.py (schema + migration با PRAGMA user_version)
  repo.py (همه‌ی کوئری‌ها)
  normalize.py، importer.py (خالص)، exporter.py (Excel + بکاپ + پاکسازی بکاپ‌های قدیمی)
  gate.py (should_gate خالص)، verify.py (منطق خالص ثبت/قفل، با زمان تزریقی)
  actions.py (کمک‌های تلگرام: دسترسی‌ها، notify، گزارش)
  texts.py (متن‌های فارسی)
  handlers/{group,private,join,admin,manage}.py
  jobs.py (sweeper دقیقه‌ای + بکاپ روزانه)
tests/
run_bot.bat
```

## جدول‌ها
- groups(chat_id PK، title، deadline_hours=24، max_attempts=5، lock_minutes=60، contact_text، report_chat_id)
- students(chat_id، code TEXT، full_name، full_name_norm، PK(chat_id, code)) — نام اختیاری
- bindings(chat_id، user_id، code، username، full_name، bound_at، PK(chat_id, user_id)، UNIQUE(chat_id, code))
- members(chat_id، user_id، status[pending|verified|revoked]، first_seen، deadline_at، unban_pending، PK(chat_id, user_id))
- attempts(chat_id، user_id، fails، locked_until، PK) — فقط برای هر کاربر؛ شمارنده‌ی هر کد (code_attempts) وجود ندارد
- bot_admins(user_id PK، added_by، added_at)
- admin_log(id، admin_id، chat_id، action، details_json، at)
- cleanup(chat_id، message_id، delete_at)

## فایل واقعی دانشجویان
فایل xlsx با یک شیت، سرستون‌های `شماره_دانشجويي` و `نام_و_نام_خانوادگي` (با «ي» عربی)، همه‌ی سلول‌ها رشته. کدها ۹ یا ۱۱ رقمی بودند؛ importer کدهای ۶ تا ۱۵ رقمی می‌پذیرد. importer سرستون را بعد از نرمال‌سازی (ي→ی، حذف `_` و فاصله) تشخیص می‌دهد؛ اگر نبود ستون اول کد است. داده‌ی واقعی دانشجوها در repo/تست کپی نمی‌شود؛ fixtureها ساختگی‌اند. فایل‌های xlsx/csv در `.gitignore` هستند.

## تصمیم‌های پیش‌فرض (کاربر تأیید کرد)
1. فقط کد دانشجویی لازم است و نام خانوادگی پرسیده نمی‌شود. نام (`full_name`) اختیاری است (`/add` تکی می‌تواند بدون نام باشد) و فقط برای نمایش به ادمین است. چون کدها ترتیبی و حدس‌زدنی‌اند، جبران: قفل تلاش برای هر کاربر، گزارش هر ثبت به ادمین‌ها، و `/unbind`.
2. اگر کد از لیست حذف شود: binding حذف و status=revoked می‌شود؛ کاربر محدود می‌شود ولی kick نمی‌شود.
3. مهلت ثبت ۲۴ ساعت، بعد kick (ban + unban فوری).
4. ادمین‌های ربات: ADMIN_IDS در .env به‌علاوه‌ی `/add_admin` (فقط ادمین‌های env می‌توانند ادمین اضافه/حذف کنند).
5. فعلاً یک گروه، ولی همه‌ی جدول‌ها chat_id دارند.

## نرمال‌سازی
اعداد فارسی/عربی → لاتین؛ ي→ی، ك→ک، ة→ه، أإآ→ا (برای نام)؛ حذف ZWNJ (U+200C)، کشیده (ـ)، اعراب (U+064B–U+0652)، فاصله‌ها؛ casefold. کد: فقط رقم پس از نرمال‌سازی، رشته (صفر اول حفظ).

## رفتار گروه
- هندلر گیت در handler group `-1` (`group.GATE_GROUP`) اجرا می‌شود: ادمین‌ها، ربات‌ها و کاربران ثبت‌شده معاف‌اند؛ پیام sender_chat و کاربران ثبت‌نکرده حذف می‌شود. اگر لیست ادمین‌ها در دسترس نباشد fail-open است.
- کاربر ثبت‌نکرده restrict (بدون هیچ مجوز) و به‌صورت pending با مهلت ثبت می‌شود. پیام راهنما/خوش‌آمد با دکمه‌ی URL به `https://t.me/<bot>?start=verify`، حداکثر یکی در هر ۲۰ ثانیه برای گروه (منشن‌ها صف می‌شوند)، و بعد از ۳ دقیقه توسط sweeper پاک می‌شود (جدول cleanup).
- `chat_member`: ورود ← restrict + pending + خوش‌آمد (مگر ثبت‌شده باشد)؛ خروج ← حذف ردیف member. `my_chat_member`: هشدار دسترسی به ADMIN_IDS. بررسی دسترسی در شروع (post_init).
- `chat_join_request` (اختیاری): ثبت‌شده ← تأیید؛ وگرنه در پیوی کد خواسته می‌شود و بعد از ثبت، `admit` درخواست را تأیید می‌کند.

## ثبت در پیوی
بدون ConversationHandler: `/start` فلگ `await_code` را می‌گذارد؛ هر متن غیردستوری یک تلاش ثبت است (برای ادمین‌ها فقط متن کدمانند یا وقتی فلگ هست). `verify_code` در `verify.py` (قالب، قفل، وجود در لیست، تعلق به حساب دیگر) و سپس `admit` (چک وضعیت عضو؛ بن‌شده‌ها دست‌نخورده) و گزارش به ادمین‌ها.

## مراحل (همه انجام شده در 0.1.0)
1. پایه: config, db, normalize, repo + تست‌ها.
2. importer/exporter و دستورات import (افزودن/جایگزینی)، `/add`، `/remove`، `/clear_list` (تأیید دوم + بکاپ)، `/export`، `/backup`.
3. دروازه‌ی گروه + sweeper (kick بعد از مهلت، retry برای unban، پاک‌کردن پیام‌ها).
4. ثبت در پیوی، `verify.py`، `admit`، join request، اطلاع قفل به ادمین.
5. `/unbind`، `/stats`، `/whois`، `/unlock`، `/verify_user`، `/settings`، `/set_contact`، `/set_deadline`، `/set_attempts`، `/set_report`، `/admins`، `/add_admin`، `/remove_admin`، `/logs`، admin_log، گزارش به محل گزارش، revoke هنگام حذف کد، `/help`.
