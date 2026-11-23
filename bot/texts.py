"""Persian user-facing texts. Numbers shown to users go through fa_num."""
from __future__ import annotations

from collections.abc import Iterable
from html import escape
from typing import Any

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(value: Any) -> str:
    """123 → '۱۲۳' (works for any value; only ASCII digits are converted)."""
    return str(value).translate(_FA_DIGITS)


def fa_list(items: Iterable[str], limit: int = 20, sep: str = "، ") -> str:
    """Join at most `limit` items, then '… و N مورد دیگر'."""
    items = list(items)
    shown = sep.join(items[:limit])
    if len(items) > limit:
        shown += f" … و {fa_num(len(items) - limit)} مورد دیگر"
    return shown


def user_label(user_id: int, username: str | None) -> str:
    return f"@{username}" if username else f"کاربر {fa_num(user_id)}"


def cmd_line(desc: str, command: str) -> str:
    """HTML line: Persian description first (keeps the line right-to-left), then the command
    in <code>. Telegram shows lines that START with /command left-to-right."""
    return f"• {desc}: <code>{escape(command)}</code>"


# ---------- general ----------
START = "سلام! 👋 من ربات احراز هویت دانشجوهای گروه هستم."
NO_ACCESS = "دسترسی ندارید."
NO_GROUP = "گروه هنوز تنظیم نشده. لطفاً GROUP_ID را در فایل .env بگذارید و ربات را دوباره اجرا کنید."
EXPIRED = "این درخواست منقضی شده. لطفاً دوباره امتحان کنید."
CANCELLED = "لغو شد. ✋"
ERROR = "مشکلی پیش آمد. لطفاً دوباره امتحان کنید."

# ---------- import ----------
IMPORT_BAD_TYPE = "فقط فایل xlsx یا csv را می‌توانم بخوانم."
IMPORT_TOO_BIG = "فایل خیلی بزرگ است (حداکثر {max_mb} مگابایت)."
IMPORT_FAILED = "نتوانستم فایل را بخوانم: {reason}"
IMPORT_NO_VALID = "هیچ کد معتبری در فایل پیدا نشد. 🤔"
BTN_ADD = "➕ افزودن به لیست"
BTN_REPLACE = "🔄 جایگزینی کامل"
BTN_CANCEL = "❌ لغو"
BTN_CLEAR_YES = "✅ بله، پاک کن"


def length_dist(counts: dict[int, int]) -> str:
    return "، ".join(f"{fa_num(n)} کد {fa_num(length)}رقمی" for length, n in counts.items())


def import_preview(
    *, filename: str, valid: int, new: int, in_db: int, to_remove: int, to_revoke: int,
    duplicates: list[tuple[int, str]], bad: list[Any], length_counts: dict[int, int],
    has_header: bool, sample: int = 10,
) -> str:
    lines = [
        f"📄 فایل «{filename}» خوانده شد.",
        f"✅ کد معتبر: {fa_num(valid)}",
        f"🆕 جدید نسبت به لیست فعلی: {fa_num(new)}",
        f"📋 لیست فعلی: {fa_num(in_db)} کد",
    ]
    if not has_header:
        lines.append("ℹ️ سرستون پیدا نشد؛ ستون اول کد فرض شد.")
    if duplicates:
        lines.append(f"🔁 تکراری داخل فایل: {fa_num(len(duplicates))}")
        lines += [f"  • ردیف {fa_num(r)}: {fa_num(c)}" for r, c in duplicates[:sample]]
    if bad:
        lines.append(f"⚠️ ردیف خراب: {fa_num(len(bad))}")
        lines += [f"  • ردیف {fa_num(b.row)}: «{b.value}» ({fa_num(b.reason)})" for b in bad[:sample]]
        if len(bad) > sample:
            lines.append(f"  … و {fa_num(len(bad) - sample)} مورد دیگر")
    if len(length_counts) > 1:
        lines.append(f"ℹ️ طول کدها یکسان نیست: {length_dist(length_counts)}")
    if to_remove:
        lines.append(
            f"⚠️ در «جایگزینی کامل» {fa_num(to_remove)} کد از لیست حذف می‌شود"
            + (f" و {fa_num(to_revoke)} نفرِ ثبت‌شده محدود می‌شوند." if to_revoke else ".")
        )
    lines.append("\nچه کنم؟")
    return "\n".join(lines)


def import_added(added: int, existing: int) -> str:
    text = f"✅ {fa_num(added)} کد به لیست اضافه شد."
    if existing:
        text += f"\n{fa_num(existing)} کد از قبل در لیست بود."
    return text


def revoked_report(bindings: list[Any]) -> str:
    if not bindings:
        return ""
    labels = [user_label(b["user_id"], b["username"]) for b in bindings]
    return (f"\n🚫 ثبت {fa_num(len(bindings))} نفر لغو شد و در گروه محدود می‌شوند: "
            f"{fa_list(labels)}")


def import_replaced(count: int, removed_bindings: list[Any], backup_name: str) -> str:
    return (f"🔄 لیست با {fa_num(count)} کد جایگزین شد.\n💾 بکاپ: {backup_name}"
            + revoked_report(removed_bindings))


# ---------- /add, /remove ----------
# Usage texts are HTML (sent with parse_mode=HTML); see cmd_line.
ADD_USAGE = ("کدها را بعد از دستور بفرستید؛ با فاصله، کاما یا در خط‌های جدا.\n"
             "هر خط می‌تواند «کد نام» باشد.\n"
             + cmd_line("مثال", "/add 400111222 علی رضایی"))
REMOVE_USAGE = ("کدها را بعد از دستور بفرستید.\n"
                + cmd_line("مثال", "/remove 400111222 400111223"))


def add_report(added: list[str], existing: list[str], bad: list[Any]) -> str:
    lines = [f"✅ اضافه شد: {fa_num(len(added))}"]
    if existing:
        lines.append(f"📋 از قبل بود: {fa_num(fa_list(existing))}")
    if bad:
        lines.append("⚠️ نامعتبر: " + fa_num(fa_list([f"«{b.value}» ({b.reason})" for b in bad], 10)))
    return "\n".join(lines)


def remove_report(removed: list[str], missing: list[str], bindings: list[Any]) -> str:
    lines = [f"🗑 حذف شد: {fa_num(len(removed))}"
             + (f" ({fa_num(fa_list(removed))})" if removed else "")]
    if missing:
        lines.append(f"❓ در لیست نبود: {fa_num(fa_list(missing))}")
    return "\n".join(lines) + revoked_report(bindings)


# ---------- /clear_list ----------
CLEAR_EMPTY = "لیست از قبل خالی است."


def clear_confirm(count: int, bound: int) -> str:
    text = f"⚠️ مطمئنید؟ همه‌ی {fa_num(count)} کد از لیست پاک می‌شود"
    if bound:
        text += f" و {fa_num(bound)} نفرِ ثبت‌شده محدود می‌شوند"
    return text + ".\nقبلش بکاپ گرفته می‌شود."


def cleared(count: int, bindings: list[Any], backup_name: str) -> str:
    return (f"🧹 {fa_num(count)} کد پاک شد.\n💾 بکاپ: {backup_name}" + revoked_report(bindings))


# ---------- /export, /backup ----------
EXPORT_CAPTION = "📊 خروجی لیست دانشجوها ({count} کد)"
BACKUP_CAPTION = "💾 بکاپ دیتابیس"


# ---------- group gate ----------
BTN_VERIFY = "🎓 ثبت کد دانشجویی"


def gate_guide(mention_html: str) -> str:
    """mention_html: one or more (comma-joined) mentions, or '' for a general notice."""
    who = f"{mention_html} " if mention_html else ""
    return (f"{who}برای نوشتن در گروه اول باید کد دانشجویی‌تان را ثبت کنید.\n"
            "روی دکمه‌ی زیر بزنید و کدتان را برای ربات بفرستید. 👇")


def welcome(mention_html: str, deadline_hours: int) -> str:
    text = (f"{mention_html} خوش آمدید! 👋\n"
            "برای نوشتن در گروه، کد دانشجویی‌تان را از طریق دکمه‌ی زیر ثبت کنید.")
    if deadline_hours > 0:  # 0 = kicking is off
        text += f"\n⏳ اگر تا {fa_num(deadline_hours)} ساعت ثبت نکنید، از گروه خارج می‌شوید."
    return text


def bot_rights_warning(missing: list[str]) -> str:
    return ("⚠️ ربات در گروه دسترسی کافی ندارد و نمی‌تواند کار کند.\n"
            "لطفاً ربات را ادمین کنید و این مجوزها را بدهید: " + "، ".join(missing))


RIGHT_ADMIN = "ادمین بودن"
RIGHT_DELETE = "حذف پیام‌ها"
RIGHT_RESTRICT = "محدودکردن اعضا (Ban users)"
BOT_REMOVED = "⚠️ ربات از گروه حذف شد یا دیگر عضو آن نیست."

# ---------- private verification ----------
ASK_CODE = "لطفاً کد دانشجویی‌تان را بفرستید (فقط رقم). ✍️"
ALREADY_VERIFIED = "شما ثبت شده‌اید ✅"
INVALID_FORMAT = ("کد دانشجویی فقط از رقم تشکیل می‌شود (بدون حرف، فاصله یا علامت).\n"
                  "مثلاً به این شکل: ۱۲۳۴۵۶۷۸۹ (این فقط یک مثال است)\n"
                  "لطفاً کد خودتان را دوباره بفرستید.")
DEFAULT_CONTACT_WHO = "ادمین‌های گروه"
NOT_FOUND_DEFAULT = "اگر دانشجوی جدید هستید یا کدتان را نمی‌دانید با {contact} تماس بگیرید."
VERIFIED_OK = "✅ کد {code} ثبت شد. حالا می‌توانید در گروه پیام بدهید."
BTN_GROUP = "ورود به گروه"
JOIN_ASK_CODE = ("سلام! 👋 درخواست عضویت شما در گروه دریافت شد.\n"
                 "برای تأیید، کد دانشجویی‌تان را همین‌جا بفرستید.")


def not_found(contact_text: str | None, default_contact: str | None) -> str:
    head = "این کد در لیست دانشجویان پیدا نشد. ❌\n"
    if contact_text:
        return head + contact_text
    return head + NOT_FOUND_DEFAULT.format(contact=default_contact or DEFAULT_CONTACT_WHO)


def _support(contact_text: str | None, default_contact: str | None) -> str:
    """Who to contact: the group's custom text, else DEFAULT_CONTACT, else the group admins."""
    if contact_text:
        return "با پشتیبانی تماس بگیرید.\n" + contact_text
    return f"با پشتیبانی تماس بگیرید: {default_contact or DEFAULT_CONTACT_WHO}"


def bound_to_other(contact_text: str | None, default_contact: str | None) -> str:
    return ("این کد قبلاً با حساب دیگری ثبت شده است.\n"
            "اگر کد مال شماست، " + _support(contact_text, default_contact))


def has_other_code(code: str, contact_text: str | None = None,
                   default_contact: str | None = None) -> str:
    return (f"شما قبلاً با کد {fa_num(code)} ثبت شده‌اید؛ "
            "برای تغییر " + _support(contact_text, default_contact))


def attempts_left(n: int) -> str:
    return f"⚠️ {fa_num(n)} تلاش دیگر باقی مانده است."


def minutes_left(seconds: int) -> str:
    return fa_num(max(1, -(-seconds // 60)))


def locked(seconds_left: int, locked_until: int | None = None) -> str:
    text = (f"⛔ به دلیل تلاش‌های ناموفق زیاد، تا {minutes_left(seconds_left)} دقیقه‌ی دیگر "
            "نمی‌توانید کد بفرستید.")
    if locked_until is not None:
        text += f"\nبعد از {tehran(locked_until)} (به وقت تهران) دوباره امتحان کنید."
    return text


# admit(): the code is saved but the user could not be let into the group (yet)
VERIFIED_SAVED = "✅ کد {code} ثبت شد."
ADMIT_RETRY = ("فعلاً نتوانستم محدودیت شما را در گروه بردارم. "
               "لطفاً چند دقیقه‌ی دیگر دوباره /start را بزنید.")


def admit_kicked(contact_text: str | None, default_contact: str | None) -> str:
    return ("شما از گروه اخراج (بن) شده‌اید و ربات نمی‌تواند شما را برگرداند.\n"
            "لطفاً " + _support(contact_text, default_contact))


def verified_report(code: str, student_name: str | None, tg_name: str | None,
                    username: str | None, user_id: int) -> str:
    parts = [f"کد {fa_num(code)}"]
    if student_name:
        parts.append(student_name)
    if tg_name:
        parts.append(tg_name)
    if username:
        parts.append(f"@{username}")
    parts.append(f"id {user_id}")
    return "✅ ثبت: " + "، ".join(parts)


def locked_report(user_id: int, username: str | None, tg_name: str | None,
                  last_code: str, minutes: int) -> str:
    who = user_label(user_id, username) + (f" ({tg_name})" if tg_name else "")
    return (f"🔒 {who} (id {user_id}) بعد از تلاش‌های ناموفق برای {fa_num(minutes)} دقیقه قفل شد.\n"
            f"آخرین کد امتحان‌شده: {fa_num(last_code)}")


# ---------- stage 5: shared ----------
def tehran(epoch: int | None) -> str:
    """Epoch → 'YYYY-MM-DD HH:MM' Tehran time with Persian digits ('—' if None)."""
    from bot.exporter import tehran_time  # local import: texts stays free of DB imports
    return fa_num(tehran_time(epoch)) if epoch is not None else "—"


def admin_action(admin_label: str, text: str) -> str:
    """An admin's change, as reported to the report chat."""
    return f"👮 {admin_label}:\n{text}"


NOT_FOUND_TARGET = "چیزی برای «{query}» پیدا نشد."

# ---------- /unbind ----------
UNBIND_USAGE = (cmd_line("استفاده", "/unbind <کد یا @username یا user_id>") + "\n"
                "اگر فقط رقم بفرستید، اول به‌عنوان کد و بعد به‌عنوان user_id جستجو می‌شود.")
UNBIND_NO_BINDING = "برای «{query}» هیچ ثبتی پیدا نشد."
UNBOUND_USER_DM = ("ثبت کد دانشجویی شما توسط ادمین لغو شد و فعلاً نمی‌توانید در گروه پیام بدهید.\n"
                   "لطفاً کد دانشجویی‌تان را دوباره همین‌جا بفرستید. ✍️")


def unbound(code: str, user_id: int, username: str | None, deadline_at: int | None) -> str:
    text = (f"🔓 ثبت کد {fa_num(code)} از {user_label(user_id, username)} (id {user_id}) "
            "آزاد شد. کاربر در گروه محدود می‌شود و باید دوباره ثبت کند.")
    if deadline_at is not None:
        text += f"\n⏳ مهلت تازه: {tehran(deadline_at)} (تهران)"
    return text


# ---------- /whois ----------
WHOIS_USAGE = (cmd_line("استفاده", "/whois <کد یا @username یا user_id>") + "\n"
               "اگر فقط رقم بفرستید، اول به‌عنوان کد و بعد به‌عنوان user_id جستجو می‌شود.")
STATUS_LABELS = {
    "pending": "در انتظار ثبت",
    "verified": "تأییدشده ✅",
    "revoked": "لغوشده (محدود) 🚫",
}


def whois(*, code: str | None, student: Any, binding: Any, user_id: int | None,
          member: Any, locked_until: int | None, fails: int) -> str:
    lines = ["🔎 نتیجه‌ی جستجو"]
    if code is not None:
        lines.append(f"🎓 کد: {fa_num(code)}")
        if student is not None:
            name = student["full_name"]
            lines.append("📋 در لیست: بله" + (f" — {name}" if name else ""))
        else:
            lines.append("📋 در لیست: نه ❌")
    if binding is not None:
        username = binding["username"]
        lines += [
            f"🔗 ثبت‌شده توسط user_id {binding['user_id']}",
            "   username: " + (f"@{username}" if username else "—"),
            "   نام تلگرام: " + (binding["full_name"] or "—"),
            f"   زمان ثبت: {tehran(binding['bound_at'])} (تهران)",
        ]
    elif code is not None:
        lines.append("🔗 این کد هنوز ثبت نشده.")
    elif user_id is not None:
        lines.append(f"👤 user_id {user_id}: هیچ کدی ثبت نکرده.")
    if user_id is not None:
        if member is None:
            lines.append("👥 وضعیت در گروه: نامشخص (عضو نیست یا هنوز دیده نشده)")
        else:
            status = STATUS_LABELS.get(member["status"], member["status"])
            lines.append(f"👥 وضعیت در گروه: {status}")
            if member["status"] == "pending":
                deadline = member["deadline_at"]
                lines.append("   مهلت ثبت: " + (f"{tehran(deadline)} (تهران)" if deadline
                                               else "بدون مهلت"))
        if locked_until is not None:
            lines.append(f"🔒 قفل تا {tehran(locked_until)} (تهران) — باز کردن: "
                         f"/unlock {user_id}")
        else:
            lines.append("🔓 قفل: نه" + (f" (تلاش ناموفق: {fa_num(fails)})" if fails else ""))
    return "\n".join(lines)


# ---------- /stats ----------
def stats(s: dict[str, int], percent: float) -> str:
    return "\n".join([
        "📊 آمار",
        f"🎓 دانشجوهای لیست: {fa_num(s['students'])}",
        f"✅ ثبت‌شده: {fa_num(s['bound'])} ({fa_num(f'{percent:.1f}')}٪)",
        f"⏳ ثبت‌نشده‌های لیست: {fa_num(max(0, s['students'] - s['bound']))}",
        f"👥 در انتظار ثبت در گروه (pending): {fa_num(s['pending'])}",
        f"🚫 لغوشده (revoked): {fa_num(s['revoked'])}",
        f"🔒 قفل‌شده در این لحظه: {fa_num(s['locked_users'])}",
    ])


# ---------- /unlock ----------
UNLOCK_USAGE = cmd_line("استفاده", "/unlock <user_id یا @username>")
UNLOCK_UNKNOWN_USERNAME = ("کاربری با username «{query}» ثبت نشده. "
                           "کاربرِ قفل‌شده معمولاً هنوز ثبت نکرده؛ لطفاً user_id را بفرستید.")


def unlocked(user_id: int, was_locked: bool) -> str:
    if was_locked:
        return f"🔓 قفل کاربر {user_id} باز شد و شمارنده‌ی تلاش‌هایش صفر شد."
    return f"کاربر {user_id} قفل نبود؛ شمارنده‌ی تلاش‌هایش صفر شد."


# ---------- /verify_user ----------
VERIFY_USER_USAGE = (cmd_line("استفاده", "/verify_user <user_id> <کد>") + "\n"
                     "کاربر را دستی به کد وصل می‌کند؛ اگر کد در لیست نباشد اضافه می‌شود.")
VERIFY_USER_BAD_ID = "user_id باید یک عدد مثبت باشد."
VERIFY_USER_BAD_CODE = "کد دانشجویی معتبر نیست (فقط رقم، ۶ تا ۱۵ رقم)."


def verify_user_bound_to_other(code: str, holder: int) -> str:
    who = f"کاربر {holder}" if holder > 0 else "کاربر دیگری"
    return (f"❌ کد {fa_num(code)} به {who} وصل است.\n"
            f"اول آن را آزاد کنید: /unbind {code}")


def verify_user_done(code: str, user_id: int, added: bool, prev_code: str | None,
                     already: bool) -> str:
    lines = []
    if added:
        lines.append(f"➕ کد {fa_num(code)} در لیست نبود و به لیست اضافه شد.")
    if already:
        lines.append(f"کاربر {user_id} از قبل با کد {fa_num(code)} ثبت بود؛ تأییدش تازه شد.")
    else:
        lines.append(f"✅ کاربر {user_id} به کد {fa_num(code)} وصل و تأیید شد.")
    if prev_code:
        lines.append(f"ℹ️ کد قبلی این کاربر ({fa_num(prev_code)}) آزاد شد.")
    lines.append("محدودیتش در گروه برداشته شد و اگر درخواست عضویت داشت، تأیید شد.")
    return "\n".join(lines)


# ---------- settings ----------
SET_CONTACT_USAGE = (cmd_line("استفاده", "/set_contact <متن>") + "\n"
                     "این متن به کسی نشان داده می‌شود که کدش در لیست نیست.\n"
                     + cmd_line("برگشت به متن پیش‌فرض", "/set_contact reset"))
CONTACT_TOO_LONG = "متن خیلی بلند است (حداکثر {max} نویسه)."
CONTACT_RESET = "✅ متن تماس به پیش‌فرض برگشت."


def contact_current(preview: str, custom: bool) -> str:
    """HTML (includes the usage); the admin-written preview is escaped."""
    head = "متن فعلی (سفارشی):" if custom else "متن فعلی (پیش‌فرض):"
    return f"{head}\n\n{escape(preview)}\n\n{SET_CONTACT_USAGE}"


def contact_set(text: str) -> str:
    return f"✅ متن تماس تنظیم شد:\n\n{text}"


SET_DEADLINE_USAGE = (cmd_line("استفاده", "/set_deadline <ساعت>") + "\n"
                      "عدد ۱ تا ۷۲۰؛ ۰ یعنی هیچ‌کس به خاطر ثبت‌نکردن اخراج نشود.")


def deadline_label(hours: int) -> str:
    return "خاموش (اخراج نمی‌شود)" if hours <= 0 else f"{fa_num(hours)} ساعت"


def deadline_current(hours: int) -> str:
    return f"مهلت فعلی: {deadline_label(hours)}\n\n{SET_DEADLINE_USAGE}"


def deadline_set(hours: int, changed_members: int) -> str:
    if hours <= 0:
        text = "✅ اخراج خودکار خاموش شد؛ کسی به خاطر ثبت‌نکردن اخراج نمی‌شود."
        if changed_members:
            text += f"\nمهلت {fa_num(changed_members)} عضوِ در انتظار برداشته شد."
        return text
    text = (f"✅ مهلت ثبت: {fa_num(hours)} ساعت (برای اعضای جدید؛ "
            "مهلتِ اعضای در انتظارِ فعلی عوض نمی‌شود).")
    if changed_members:
        text += f"\n{fa_num(changed_members)} عضوِ در انتظارِ بدون مهلت، مهلت تازه گرفتند."
    return text


SET_ATTEMPTS_USAGE = (cmd_line("استفاده", "/set_attempts <تعداد> [دقیقه‌ی قفل]") + "\n"
                      "تعداد ۱ تا ۲۰، دقیقه‌ی قفل ۱ تا ۱۰۰۸۰ (یک هفته).\n"
                      + cmd_line("مثال", "/set_attempts 5 60"))


def attempts_current(max_attempts: int, lock_minutes: int) -> str:
    return (f"فعلی: {fa_num(max_attempts)} تلاش، قفل {fa_num(lock_minutes)} دقیقه\n\n"
            + SET_ATTEMPTS_USAGE)


def attempts_set(max_attempts: int, lock_minutes: int) -> str:
    return (f"✅ بعد از {fa_num(max_attempts)} تلاش ناموفق، کاربر "
            f"{fa_num(lock_minutes)} دقیقه قفل می‌شود.")


SET_REPORT_USAGE = ("استفاده:\n"
                    + cmd_line("در پیوی (ربات باید عضو آن گروه/کانال باشد)",
                               "/set_report <chat_id>") + "\n"
                    + cmd_line("یا در گروه ادمین‌ها خودتان بزنید", "/set_report") + "\n"
                    + cmd_line("گزارش‌ها به پیوی ادمین‌های اصلی برود", "/set_report off"))
REPORT_BAD_ID = "chat_id باید عدد باشد (مثلاً ‎-1001234567890)."
REPORT_NOT_MAIN_GROUP = ("گروه اصلی دانشجوها نمی‌تواند محل گزارش باشد "
                         "(اطلاعات دانشجوها لو می‌رود).")
REPORT_TEST = "✅ این چت محل گزارش‌های ربات احراز هویت شد."
REPORT_OFF = "✅ گزارش به گروه خاموش شد؛ گزارش‌ها به پیوی ادمین‌های اصلی (ADMIN_IDS) می‌رود."


def report_send_failed(chat_id: int, reason: str) -> str:
    return (f"❌ نتوانستم به {chat_id} پیام بدهم ({reason}).\n"
            "ربات را عضو آن گروه/کانال کنید (در کانال باید ادمین باشد) و دوباره امتحان کنید.")


def report_set(chat_id: int) -> str:
    return f"✅ گزارش‌ها از این پس به چت {chat_id} فرستاده می‌شود."


def report_current(report_chat_id: int | None) -> str:
    where = f"چت {report_chat_id}" if report_chat_id else "پیوی ادمین‌های اصلی (ADMIN_IDS)"
    return f"محل فعلی گزارش‌ها: {where}\n\n{SET_REPORT_USAGE}"


def report_channel_hint(chat_id: int) -> str:
    return ("در کانال نمی‌توانم بفهمم چه کسی پیام داده، پس از این‌جا تنظیم نمی‌کنم.\n"
            f"در پیوی ربات این را بفرستید:\n/set_report {chat_id}")


def settings(*, group_id: int, title: str | None, deadline_hours: int, max_attempts: int,
             lock_minutes: int, contact_text: str | None, report_chat_id: int | None,
             default_contact: str | None) -> str:
    contact = contact_text or ("(پیش‌فرض) " + NOT_FOUND_DEFAULT.format(
        contact=default_contact or DEFAULT_CONTACT_WHO))
    report = f"چت {report_chat_id}" if report_chat_id else "پیوی ادمین‌های اصلی"
    return "\n".join([
        "⚙️ تنظیمات",
        f"👥 گروه: {title or '—'} ({group_id})",
        f"⏳ مهلت ثبت: {deadline_label(deadline_hours)}  ← /set_deadline",
        f"🔒 قفل: بعد از {fa_num(max_attempts)} تلاش ناموفق، {fa_num(lock_minutes)} دقیقه"
        "  ← /set_attempts",
        f"📞 متن تماس: {contact}  ← /set_contact",
        f"📣 گزارش‌ها: {report}  ← /set_report",
    ])


# ---------- bot admins ----------
ADD_ADMIN_USAGE = cmd_line("استفاده", "/add_admin <user_id>")
REMOVE_ADMIN_USAGE = cmd_line("استفاده", "/remove_admin <user_id>")
ONLY_OWNERS = "فقط ادمین‌های اصلی (ADMIN_IDS در .env) می‌توانند ادمین اضافه یا حذف کنند."
ADMIN_IS_OWNER_ADD = "این کاربر از قبل ادمین اصلی (ADMIN_IDS) است."
ADMIN_IS_OWNER_REMOVE = "ادمین‌های اصلی (ADMIN_IDS) را نمی‌شود حذف کرد؛ فقط از فایل .env."
ADMIN_ALREADY = "این کاربر از قبل ادمین است."
ADMIN_NOT_FOUND = "این کاربر ادمین نیست."


def admin_added(user_id: int) -> str:
    return f"✅ کاربر {user_id} ادمین ربات شد."


def admin_removed(user_id: int) -> str:
    return f"🗑 کاربر {user_id} دیگر ادمین ربات نیست."


def admins_list(owner_ids: Iterable[int], db_admins: list[Any]) -> str:
    owners = [f"  • {uid}" for uid in sorted(owner_ids)] or ["  —"]
    added = [f"  • {r['user_id']} (توسط {r['added_by'] or '—'}، {tehran(r['added_at'])})"
             for r in db_admins] or ["  —"]
    return "\n".join(["👮 ادمین‌های ربات", "", "ادمین‌های اصلی (.env):", *owners,
                      "", "اضافه‌شده با /add_admin:", *added])


# ---------- /logs ----------
LOGS_USAGE = cmd_line("استفاده (پیش‌فرض ۲۰، حداکثر ۱۰۰)", "/logs [تعداد]")
LOGS_EMPTY = "هنوز هیچ عملی ثبت نشده."


def log_line(row: Any, max_details: int = 200) -> str:
    details = row["details_json"] or ""
    if len(details) > max_details:
        details = details[:max_details] + "…"
    return (f"#{row['id']} {tehran(row['at'])} | admin {row['admin_id']} | {row['action']}"
            + (f" | {details}" if details else ""))


# ---------- /help ----------
HELP_USER = ("🎓 راهنما\n"
             "برای نوشتن در گروه، کد دانشجویی‌تان را (فقط رقم) همین‌جا بفرستید.\n"
             "اگر کدتان پیدا نشد یا با حساب دیگری ثبت شده، با ادمین‌های گروه تماس بگیرید.\n"
             "بعد از چند تلاش ناموفق، مدتی قفل می‌شوید.")

# HTML (parse_mode=HTML): every line starts with Persian so it stays right-to-left.
HELP_ADMIN = "\n".join([
    "📖 راهنمای ادمین (دستورات فقط در پیوی ربات کار می‌کنند)",
    "",
    "📋 لیست دانشجوها",
    "• فرستادن فایل xlsx یا csv ← پیش‌نمایش، بعد «افزودن» یا «جایگزینی کامل»",
    "  (ستون کد دانشجویی لازم است، ستون نام اختیاری)",
    cmd_line("افزودن؛ چند کد با فاصله/کاما یا هر خط یک کد", "/add کد [نام]"),
    cmd_line("حذف؛ ثبتِ آن کدها لغو و کاربرشان محدود می‌شود", "/remove کد…"),
    cmd_line("پاک کردن کل لیست (با تأیید و بکاپ)", "/clear_list"),
    cmd_line("خروجی Excel", "/export"),
    cmd_line("بکاپ دیتابیس", "/backup"),
    "",
    "👤 کاربران",
    cmd_line("دانشجو، ثبت، وضعیت و قفل", "/whois <کد|@username|user_id>"),
    cmd_line("آزاد کردن ثبت؛ کاربر محدود می‌شود و باید دوباره ثبت کند",
             "/unbind <کد|@username|user_id>"),
    cmd_line("وصل کردن دستی (کدِ ناموجود به لیست اضافه می‌شود)", "/verify_user <user_id> <کد>"),
    cmd_line("باز کردن قفل تلاش‌ها", "/unlock <user_id|@username>"),
    cmd_line("آمار ثبت‌نام", "/stats"),
    "ورودیِ فقط‌رقمی اول به‌عنوان کد و بعد به‌عنوان user_id جستجو می‌شود.",
    "",
    "⚙️ تنظیمات",
    cmd_line("نمایش همه‌ی تنظیمات", "/settings"),
    cmd_line("مهلت ثبت (۱ تا ۷۲۰؛ ۰ = اخراج خاموش)", "/set_deadline <ساعت>"),
    cmd_line("تلاش ناموفق تا قفل، و مدت قفل", "/set_attempts <تعداد> [دقیقه]"),
    cmd_line("متن کدِ پیدانشده (reset = پیش‌فرض)", "/set_contact <متن>"),
    cmd_line("محل گزارش‌ها (یا همین دستور را در گروه ادمین‌ها بزنید)",
             "/set_report <chat_id|off>"),
    "",
    "👮 ادمین‌ها",
    cmd_line("لیست ادمین‌ها", "/admins"),
    cmd_line("افزودن ادمین؛ فقط ادمین‌های اصلی (.env)", "/add_admin <user_id>"),
    cmd_line("حذف ادمین؛ فقط ادمین‌های اصلی (.env)", "/remove_admin <user_id>"),
    "",
    "🧾 لاگ",
    cmd_line("n عمل آخر ادمین‌ها (پیش‌فرض ۲۰، حداکثر ۱۰۰)", "/logs [n]"),
    "",
    "هر تغییر در لاگ ثبت و به محل گزارش فرستاده می‌شود.",
])

# Bot menu (set_my_commands); descriptions must be 1-256 chars.
USER_COMMANDS = [
    ("start", "شروع و ثبت کد دانشجویی"),
    ("help", "راهنما"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    ("whois", "اطلاعات یک کد یا کاربر"),
    ("unbind", "آزاد کردن ثبت یک کد/کاربر"),
    ("verify_user", "وصل کردن دستی کاربر به کد"),
    ("unlock", "باز کردن قفل کاربر"),
    ("stats", "آمار ثبت‌نام"),
    ("add", "افزودن کد به لیست"),
    ("remove", "حذف کد از لیست"),
    ("clear_list", "پاک کردن کل لیست"),
    ("export", "خروجی Excel"),
    ("backup", "بکاپ دیتابیس"),
    ("settings", "نمایش تنظیمات"),
    ("set_deadline", "مهلت ثبت (ساعت)"),
    ("set_attempts", "تعداد تلاش و مدت قفل"),
    ("set_contact", "متن تماس برای کد ناموجود"),
    ("set_report", "محل گزارش‌ها"),
    ("admins", "لیست ادمین‌ها"),
    ("logs", "آخرین عمل‌های ادمین‌ها"),
]
OWNER_COMMANDS = ADMIN_COMMANDS + [
    ("add_admin", "افزودن ادمین ربات"),
    ("remove_admin", "حذف ادمین ربات"),
]
