"""Локальное время пользователя. created_at в БД остаётся UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Almaty"

# Короткий список для /settings: типичные пояса пользователей бота.
TIMEZONE_CHOICES: dict[str, str] = {
    "Asia/Almaty": "Алматы (UTC+5)",
    "Asia/Aqtobe": "Актобе (UTC+5)",
    "Europe/Moscow": "Москва (UTC+3)",
    "UTC": "UTC",
}


def normalize_timezone(name: str | None) -> str:
    if name in TIMEZONE_CHOICES:
        return name
    return DEFAULT_TIMEZONE


def _zone(name: str) -> ZoneInfo:
    tz_name = normalize_timezone(name)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_in_tz(tz_name: str, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(_zone(tz_name))


def today_in_tz(tz_name: str, now: datetime | None = None) -> date:
    return now_in_tz(tz_name, now).date()
