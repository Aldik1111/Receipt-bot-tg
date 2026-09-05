# Production runbook

Этот файл про сервер, не про фичи бота. Юридическая оферта, смена
секретов и чистка git-истории сюда не входят.

## Docker Compose

Нужны `.env` (с `BOT_TOKEN`, `GEMINI_API_KEY`, при Mini App ещё `WEBAPP_URL`
и `DOMAIN`) и Docker.

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot webapp
```

SQLite живёт в volume `sqlite-data` (`/data/budget.db`). Логи и серверные
снимки — там же: `/data/logs`, `/data/backups`.

Caddy слушает 80/443, отдаёт `webapp/` и проксирует `/api/*` и `/health`
на webapp. Для настоящего HTTPS выставь `DOMAIN=твой.домен`.

Без Docker те же unit-файлы лежат в `deploy/`.

## Health

- Бот: `python scripts/healthcheck.py --db-only` (SQLite + сердцебиение планировщиков).
- Mini App: `GET /health` → `{"status":"ok"|"degraded","db":"ok",...}`.
- Снаружи: `python scripts/healthcheck.py --url https://DOMAIN/health`.

`degraded` значит база жива, но какой-то планировщик давно не отмечался.
Это не повод сразу рестартить том с базой — сначала логи.

## Логи

`LOG_FORMAT=json` в контейнерах. Поля `amount`, `store`, `description`,
`token`, `initData` в extra режутся. Ротация: `TimedRotatingFileHandler`,
`LOG_RETAIN_DAYS` (по умолчанию 14).

```bash
docker compose logs --tail=200 bot
```

## Бэкап и restore

Раз в сутки бот пишет `backups/budget-YYYYMMDD-HHMMSS.db` и удаляет файлы
старше `BACKUP_RETAIN_DAYS`. Вручную:

```bash
python scripts/backup_sqlite.py
python scripts/backup_sqlite.py --restore-check backups/budget-YYYYMMDD-HHMMSS.db
```

Вернуть снимок (бот и webapp должны быть остановлены):

```bash
docker compose stop bot webapp
# скопировать выбранный budget-*.db поверх /data/budget.db в volume
docker compose start bot webapp
```

Откат кода: предыдущий образ/коммит + тот же снимок базы, если схема
уже убежала вперёд. Пользовательский JSON-бэкап из `/settings` — это
данные одного Telegram-аккаунта, не файл сервера.

## Типичные сбои

| Симптом | Что проверить |
|---|---|
| Бот молчит | `BOT_TOKEN`, `docker compose logs bot`, не занят ли polling другим процессом |
| 401/403 в Mini App | `WEBAPP_URL` совпадает с BotFather, `BOT_TOKEN` один и тот же у bot и webapp |
| Чеки не распознаются | `GEMINI_API_KEY`, суточный лимит пользователя (`/pro`), логи Gemini |
| `database is locked` | оба процесса смотрят в один `DB_PATH`, volume смонтирован, диск не кончился |
| Health `degraded` | какой планировщик stale, не завис ли event loop |
| Диск растёт | `LOG_RETAIN_DAYS`, `BACKUP_RETAIN_DAYS`, WAL-файлы рядом с `budget.db` |
| После рестарта пустая база | volume не тот / `DB_PATH` указывает не в `/data` |
