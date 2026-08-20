"""Хранилище FSM для aiogram поверх той же SQLite, что и остальные данные.

Штатный MemoryStorage держит шаг диалога в оперативке: после рестарта бот
забывал, что пользователь на середине /add или вводит лимит бюджета, и
следующее сообщение улетало в быстрый ввод ("500" превращалось в трату).
Redis ради одного личного бота ставить не хочется, поэтому состояние живёт
в таблице app_state рядом с черновиками.

Одна строка на StorageKey, внутри JSON вида {"state": ..., "data": {...}}.
"""

from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

import db

SCOPE = "fsm"


def _row_key(key: StorageKey) -> str:
    """Плоский ключ строки. Порядок полей фиксирован - он же уникальность."""
    return ":".join(
        str(part) if part is not None else ""
        for part in (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        )
    )


class SQLiteStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        state_str = state.state if isinstance(state, State) else state
        record = db.load_state(SCOPE, _row_key(key)) or {}
        record["state"] = state_str
        self._save(key, record)

    async def get_state(self, key: StorageKey) -> str | None:
        record = db.load_state(SCOPE, _row_key(key)) or {}
        return record.get("state")

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        record = db.load_state(SCOPE, _row_key(key)) or {}
        record["data"] = dict(data)
        self._save(key, record)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = db.load_state(SCOPE, _row_key(key)) or {}
        return dict(record.get("data") or {})

    async def close(self) -> None:
        """Соединения открываются на каждый запрос в db.get_conn - закрывать нечего."""

    @staticmethod
    def _save(key: StorageKey, record: dict[str, Any]) -> None:
        # Диалог завершён (state.clear()) - строку не держим, чтобы таблица не
        # копила по записи на каждого, кто когда-либо нажал кнопку.
        if not record.get("state") and not record.get("data"):
            db.delete_state(SCOPE, _row_key(key))
            return
        db.save_state(SCOPE, _row_key(key), record, user_id=key.user_id)
