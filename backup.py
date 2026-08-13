"""Личный бэкап пользователя в JSON - только его данные, не вся общая база
SQLite (там были бы данные всех пользователей бота)."""

import io
import json

import db


def build_backup(user_id: int) -> io.BytesIO:
    data = db.get_all_user_rows(user_id)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return io.BytesIO(text.encode("utf-8"))