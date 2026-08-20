"""
Восстановление данных пользователя из JSON-бэкапа (того, что делает backup.py
и присылает в /settings -> "Скачать бэкап" или в еженедельном автобэкапе).

Использование:
    python3 restore_backup.py <user_id> <путь_к_backup.json>

Например:
    python3 restore_backup.py 123456789 backup_2026-08-20.json

Восстановление идёт одной транзакцией и заменяет текущие данные пользователя
тем, что в файле. Повторный запуск того же файла не плодит дубли.
"""

import json
import sys

import db


def restore(user_id: int, backup_path: str) -> None:
    db.init_db()
    with open(backup_path, encoding="utf-8") as f:
        data = json.load(f)

    print(f"Восстанавливаю данные пользователя {user_id} из {backup_path}...")
    counts = db.restore_user_backup(user_id, data)
    print(f"  Операций: {counts['transactions']}")
    print(f"  Чеков: {counts['receipts']}")
    print(f"  Повторов: {counts['recurring']}")
    print(f"  Целей: {counts['goals']}")
    print("Готово.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python3 restore_backup.py <user_id> <путь_к_backup.json>")
        sys.exit(1)
    restore(int(sys.argv[1]), sys.argv[2])
