#!/usr/bin/env bash
# One-line installer for Ubuntu/Debian servers:
#   curl -fsSL https://raw.githubusercontent.com/amor7/university-group-bot/main/install.sh | sudo bash
# Installs the bot into /opt/student-bot, runs it as a systemd service (24/7, starts on boot,
# restarts on crash) and adds the "student-bot" management command.
# Running it again updates the code and keeps .env, the database and backups.
# Non-interactive install: pass the settings as environment variables, e.g.
#   curl -fsSL .../install.sh | sudo BOT_TOKEN=... ADMIN_IDS=... GROUP_ID=... bash
set -euo pipefail

# Everything is inside main() so a half-downloaded script never runs.
main() {
REPO_URL="${REPO_URL:-https://github.com/amor7/university-group-bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/student-bot}"
SERVICE="student-bot"
BOT_USER="studentbot"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
die() { red "خطا: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "با sudo اجرا کنید: curl -fsSL ... | sudo bash"
command -v apt-get >/dev/null || die "این اسکریپت برای Ubuntu/Debian است."

# Questions are read from the terminal even when the script itself comes through a pipe.
HAS_TTY=0
if (exec </dev/tty) 2>/dev/null; then HAS_TTY=1; fi
ask() {  # ask VAR "question"  (keeps a value already given in the environment)
  local var="$1" prompt="$2" ans=""
  if [ -n "${!var:-}" ]; then return; fi
  if [ "$HAS_TTY" -eq 1 ]; then
    read -r -p "$prompt: " ans </dev/tty || true
  fi
  printf -v "$var" '%s' "$ans"
}
quote() {  # a single-quoted .env value (python-dotenv: \\ and \' are the only escapes)
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//\'/\\\'}"
  printf "'%s'" "$v"
}

green "۱/۶ نصب پیش‌نیازها (python3، venv، git)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates nano >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 یا جدیدتر لازم است (Ubuntu 22.04 به بالا)."

green "۲/۶ دریافت کد ربات..."
id "$BOT_USER" >/dev/null 2>&1 || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$BOT_USER"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" fetch -q origin "$BRANCH"
  git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" reset -q --hard "origin/$BRANCH"
  yellow "کد به‌روز شد (.env، دیتابیس و بکاپ‌ها دست نخوردند)."
else
  mkdir -p "$INSTALL_DIR"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

green "۳/۶ ساخت محیط پایتون و نصب کتابخانه‌ها..."
[ -x "$INSTALL_DIR/.venv/bin/python" ] || python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

green "۴/۶ تنظیمات (.env)..."
ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  yellow "فایل .env از قبل هست و دست نخورد. برای تغییر: sudo student-bot config"
else
  if [ "$HAS_TTY" -eq 0 ] && [ -z "${BOT_TOKEN:-}" ]; then
    die "ترمینال در دسترس نیست. BOT_TOKEN، ADMIN_IDS و GROUP_ID را به‌صورت متغیر محیطی بدهید."
  fi
  echo "اطلاعات زیر را وارد کنید (راهنمای کامل در README مخزن):"
  ask BOT_TOKEN "توکن ربات از @BotFather"
  [ -n "$BOT_TOKEN" ] || die "توکن ربات لازم است."
  ask ADMIN_IDS "آیدی عددی ادمین‌های اصلی (با کاما جدا)"
  [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "ADMIN_IDS باید عدد باشد، مثل 123456789 یا 123,456"
  ask GROUP_ID "آیدی گروه (مثل -1001234567890)"
  [[ "$GROUP_ID" =~ ^-[0-9]+$ ]] || die "GROUP_ID باید عددی منفی باشد، مثل -1001234567890"
  ask DEFAULT_CONTACT "متن تماس برای دانشجوی پیدانشده (اختیاری، Enter برای رد شدن)"
  ask PROXY_URL "پروکسی تلگرام اگر سرور در ایران است (اختیاری، مثل socks5://127.0.0.1:1080)"
  umask 077
  {
    echo "BOT_TOKEN=$(quote "$BOT_TOKEN")"
    echo "ADMIN_IDS=$ADMIN_IDS"
    echo "DB_PATH=bot.db"
    echo "GROUP_ID=$GROUP_ID"
    echo "DEFAULT_CONTACT=$(quote "$DEFAULT_CONTACT")"
    echo "PROXY_URL=$(quote "$PROXY_URL")"
  } > "$ENV_FILE"
  umask 022
fi
chown -R "$BOT_USER":"$BOT_USER" "$INSTALL_DIR"
chmod 600 "$ENV_FILE"

green "۵/۶ ساخت سرویس systemd (اجرای ۲۴ ساعته و روشن شدن بعد از ریستارت)..."
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Telegram student verification bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

TMP_CMD="$(mktemp)"
cat > "$TMP_CMD" <<EOF
#!/usr/bin/env bash
# Management command for the student bot.
set -e
case "\${1:-help}" in
  status)  systemctl status $SERVICE --no-pager ;;
  start)   sudo systemctl start $SERVICE ;;
  stop)    sudo systemctl stop $SERVICE ;;
  restart) sudo systemctl restart $SERVICE ;;
  logs)    sudo journalctl -u $SERVICE -n "\${2:-100}" --no-pager ;;
  follow)  sudo journalctl -u $SERVICE -f ;;
  config)  sudo nano $INSTALL_DIR/.env && sudo systemctl restart $SERVICE ;;
  update)  curl -fsSL https://raw.githubusercontent.com/amor7/university-group-bot/$BRANCH/install.sh | sudo bash; exit \$? ;;
  backups) sudo ls -lh $INSTALL_DIR/backups ;;
  *) echo "student-bot status|start|stop|restart|logs [n]|follow|config|update|backups" ;;
esac
EOF
chmod 755 "$TMP_CMD"
mv -f "$TMP_CMD" /usr/local/bin/student-bot

green "۶/۶ روشن کردن ربات..."
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 5
if systemctl is-active -q "$SERVICE"; then
  green "✅ ربات روشن است و بعد از ریستارت سرور هم خودکار بالا می‌آید."
else
  red "ربات روشن نشد. لاگ را ببینید: student-bot logs"
fi
cat <<EOF

دستورهای مدیریت:
  student-bot status     وضعیت
  student-bot logs       ۱۰۰ خط آخر لاگ
  student-bot follow     لاگ زنده
  student-bot restart    ریستارت
  student-bot config     ویرایش .env و ریستارت
  student-bot update     به‌روزرسانی به آخرین نسخه
  student-bot backups    لیست بکاپ‌ها
پوشه‌ی نصب: $INSTALL_DIR
EOF
}

main "$@"
