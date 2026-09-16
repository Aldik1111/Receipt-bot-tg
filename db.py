"""
Слой работы с базой данных (SQLite). Фасад пакета storage: handlers и тесты
по-прежнему делают `import db`.
"""

from storage import (  # noqa: F401
    backup,
    books,
    budgets,
    catalog,
    conn,
    goals,
    quota,
    recurring,
    schema,
    state,
    transactions,
    users,
    wipe,
)

_MODULES = (
    conn,
    schema,
    catalog,
    transactions,
    recurring,
    budgets,
    goals,
    users,
    quota,
    state,
    wipe,
    backup,
    books,
)


def _export_all() -> None:
    for mod in _MODULES:
        for name in dir(mod):
            if name.startswith("__"):
                continue
            globals()[name] = getattr(mod, name)


def _wire_storage() -> None:
    exported = {key: value for key, value in globals().items() if not key.startswith("__")}
    for mod in _MODULES:
        for key, value in exported.items():
            setattr(mod, key, value)


_export_all()
_wire_storage()
