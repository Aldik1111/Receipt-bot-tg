FROM python:3.12-slim-bookworm

WORKDIR /app

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app \
    && mkdir -p /data /data/backups /data/logs \
    && chown -R app:app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/budget.db \
    BACKUP_DIR=/data/backups \
    LOG_FILE=/data/logs/app.log \
    LOG_FORMAT=json

EXPOSE 8000

CMD ["python", "bot.py"]
