# کارهای باز

موارد انجام‌شده را از اینجا حذف کنید.

## امنیت
- zip bomb در xlsx: حجم بازشده را چک کنید و خواندن فایل را با `asyncio.to_thread` از حلقه‌ی رویداد جدا کنید.
- formula injection در export (سلول‌هایی که با `=`، `+`، `-`، `@` شروع می‌شوند).
- `/set_report`، `/export` و `/backup` را به owner (ADMIN_IDS) محدود کنید.
- پیام BOUND_TO_OTHER و NOT_FOUND را یکسان کنید تا oracle نشود، یا سقف کلی شکست بگذارید.

## معماری
- helperهای مشترک را از `admin.py` به `handlers/common.py` ببرید.
- `now()` در چند ماژول تکراری است.
- `restrict_all` تکراری است.
- `is_registered` به `verify.py` منتقل شود.
- `unbind_code` بلااستفاده حذف شود.
- `update_group_setting` و `verify_user` تراکنش‌های جدا دارند؛ یکی شوند.
- `manage.py` بزرگ است؛ بشکنید.

## UX
- چیدمان دکمه‌های import و تأیید دوم برای replace وقتی کسی revoke می‌شود.
- یادآوری قبل از kick و پیام بعد از آن.
- تاریخ شمسی.
- «٫» فارسی در درصد (`/stats`).
- تکرار id در گزارش قفل.
- مثال چندخطی برای `/add`.
- مسیر بکاپ در پیام.
- `/cancel`.
- منشن‌های صف‌شده فقط با پیام بعدیِ گروه ارسال می‌شوند.

## عملیات
- CI.
- pip-audit.
- pytest-cov.
- `/health`.
- کپی بکاپ بیرون از سرور.

## رفتار
- `on_chat_member` در صورت شکست `get_chat` هنوز با BASIC_PERMISSIONS unrestrict می‌کند.
- `cmd_verify_user` نتیجه‌ی admit (kicked) را نادیده می‌گیرد.
- `rights_warning` حق `can_invite_users` را چک نمی‌کند.
- اگر ربات حق ban را از دست بدهد، pendingها بی‌صدا حذف می‌شوند.

## ایده‌ها
- درخواست بررسی دستی با دکمه برای دانشجوی جدید.
- دکمه‌ی تماس با URL.
- AIORateLimiter.

## یافته‌های جزئی دور دوم بازبینی (۲۰۲۶-۱۱-۲۳)
- UX: `guide_due` حتی وقتی throttle پیامی نفرستاده ثبت می‌شود؛ کاربر دوم در همان ۲۰ ثانیه تا ۱۰ دقیقه راهنما نمی‌بیند. یک job کوتاه برای تخلیه‌ی صف منشن‌ها.
- عملیات: `run_bot.bat` در خطای تنظیمات (BOT_TOKEN خالی) بی‌پایان هر ۱۰ ثانیه تکرار می‌شود؛ با exit code تنظیمات حلقه را بشکند.
- عملیات: `retry_unbans` در شکست مداوم هر دقیقه log.error می‌زند (نویز).
- رفتار: تشخیص خطای rights/not-member با متن پیام تلگرام است؛ در اولین اجرای واقعی لاگ‌ها چک شود.
- تست: شاخه‌های race `CodeAlreadyBound`/`StudentNotFound` در verify.py تست مستقیم ندارند.
