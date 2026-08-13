"""
Восстановление данных пользователя из JSON-бэкапа (того, что делает backup.py
и присылает в /settings -> "Скачать бэкап" или в еженедельном автобэкапе).

Использование:
    python3 restore_backup.py <user_id> <путь_к_backup.json>

Например:
    python3 restore_backup.py 123456789 backup_2026-08-20.json

Скрипт идемпотентен для категорий/способов оплаты (не создаст дублей при
повторном запуске благодаря UNIQUE-ограничениям в БД), но операции/бюджеты/
цели при повторном запуске задублируются - запускай один раз на чистую базу.
"""

import json
import sys

import db


def restore(user_id: int, backup_path: str) -> None:
    db.init_db()
    db.ensure_user(user_id, None)  # создаёт дефолтные категории/способы оплаты

    with open(backup_path, encoding="utf-8") as f:
        data = json.load(f)

    print(f"Восстанавливаю данные пользователя {user_id} из {backup_path}...")

    for cat in data.get("categories", []):
        db.add_category(user_id, cat["name"], cat.get("emoji", "🏷"))
    print(f"  Категорий: {len(data.get('categories', []))}")

    for pm in data.get("payment_methods", []):
        db.add_payment_method(user_id, pm["name"])
    print(f"  Способов оплаты: {len(data.get('payment_methods', []))}")

    tx_count = 0
    for tx in data.get("transactions", []):
        cat_id = db.get_category_id_by_name(user_id, tx["category"]) if tx.get("category") else None
        pm_id = db.get_payment_method_id_by_name(user_id, tx["payment_method"]) if tx.get("payment_method") else None
        db.add_transaction(
            user_id=user_id,
            tx_type=tx["type"],
            amount=tx["amount"],
            category_id=cat_id,
            payment_method_id=pm_id,
            store=tx.get("store"),
            description=tx.get("description"),
            op_date=tx["op_date"],
            op_time=tx.get("op_time"),
        )
        tx_count += 1
    print(f"  Операций: {tx_count}")

    for b in data.get("budgets", []):
        cat_id = db.get_category_id_by_name(user_id, b["category"])
        if cat_id:
            db.set_budget(user_id, cat_id, b["monthly_limit"])
    print(f"  Бюджетов: {len(data.get('budgets', []))}")

    for g in data.get("goals", []):
        goal_id = db.create_goal(user_id, g["name"], g["target_amount"])
        if g.get("current_amount"):
            db.contribute_to_goal(user_id, goal_id, g["current_amount"])
    print(f"  Целей: {len(data.get('goals', []))}")

    print("Готово.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python3 restore_backup.py <user_id> <путь_к_backup.json>")
        sys.exit(1)
    restore(int(sys.argv[1]), sys.argv[2])
