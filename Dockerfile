# Container image for Docker hosts / PaaS (Liara, Railway, Render, Fly.io, any VPS with Docker).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Tehran DB_PATH=/app/data/bot.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN useradd --system --uid 10001 bot && mkdir -p /app/data /app/logs
# data/ holds the database and backups/, logs/ the rotating log: keep both on volumes.
VOLUME ["/app/data", "/app/logs"]
# Volumes are often created by root (bind mounts, PaaS disks): fix ownership, then drop to "bot".
ENTRYPOINT ["sh", "-c", "chown -R 10001:10001 /app/data /app/logs && exec setpriv --reuid=10001 --regid=10001 --init-groups python -m bot.main"]
