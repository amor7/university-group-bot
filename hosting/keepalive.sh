#!/usr/bin/env bash
# For shared hosting (cPanel/DirectAdmin) that has SSH + Python but no systemd.
# Starts the bot if it is not running. Add it to cron every 5 minutes:
#   */5 * * * * /bin/bash $HOME/student-bot/hosting/keepalive.sh >/dev/null 2>&1
# Also add it with @reboot if the host allows that.
cd "$(dirname "$0")/.." || exit 1
DIR="$(pwd)"
PIDFILE="logs/bot.pid"
mkdir -p logs
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE")"
  # the pid must still be our bot (after a reboot it may belong to another process)
  if kill -0 "$pid" 2>/dev/null && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "bot.main"; then
    exit 0
  fi
fi
# keep stdout.log small
if [ -f logs/stdout.log ] && [ "$(wc -c < logs/stdout.log)" -gt 5000000 ]; then
  mv -f logs/stdout.log logs/stdout.log.1
fi
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
nohup "$PY" -m bot.main >> logs/stdout.log 2>&1 &
echo $! > "$PIDFILE"
