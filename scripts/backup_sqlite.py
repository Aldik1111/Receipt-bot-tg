"""Copy the live SQLite database and verify the copy in a separate file.

Usage:
    python scripts/backup_sqlite.py
    python scripts/backup_sqlite.py --restore-check backups/budget-YYYYMMDD-HHMMSS.db

The copy itself is written under backups/ and is gitignored: it contains
real user data and must not be committed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import BACKUP_DIR, BACKUP_RETAIN_DAYS, DB_PATH  # noqa: E402


INTERESTING_TABLES = (
    "users",
    "transactions",
    "receipts",
    "recurring_payments",
    "recurring_runs",
    "savings_goals",
    "category_budgets",
    "categories",
    "payment_methods",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_counts(path: Path) -> dict[str, int]:
    conn = _connect(path)
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {}
        for table in INTERESTING_TABLES:
            if table not in existing:
                counts[table] = 0
                continue
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts
    finally:
        conn.close()


def integrity_ok(path: Path) -> str:
    conn = _connect(path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def copy_database(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        backup = sqlite3.connect(dest)
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()


def backups_dir() -> Path:
    path = Path(BACKUP_DIR)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def prune_old_backups(directory: Path | None = None, retain_days: int = BACKUP_RETAIN_DAYS) -> int:
    folder = directory or backups_dir()
    cutoff = datetime.now().timestamp() - retain_days * 86400
    removed = 0
    for path in folder.glob("budget-*.db"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def create_timestamped_backup() -> Path | None:
    src = Path(DB_PATH)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups_dir() / f"budget-{stamp}.db"
    copy_database(src, dest)
    verify_copy(src, dest)
    prune_old_backups()
    return dest


def restore_check(backup_path: Path, dest: Path) -> None:
    """Копирует снимок в отдельный файл и сверяет его с самим собой, не с live DB."""
    if dest.exists():
        dest.unlink()
    copy_database(backup_path, dest)
    dest_check = integrity_ok(dest)
    if dest_check != "ok":
        raise SystemExit(f"Restored copy failed integrity_check: {dest_check}")
    src_counts = table_counts(backup_path)
    dest_counts = table_counts(dest)
    if src_counts != dest_counts:
        raise SystemExit(
            f"Row counts differ.\n  backup: {src_counts}\n  restored: {dest_counts}"
        )
    print(f"integrity restored={dest_check}")
    for table, count in src_counts.items():
        print(f"  {table}: {count}")


def verify_copy(src: Path, dest: Path) -> None:
    src_check = integrity_ok(src)
    dest_check = integrity_ok(dest)
    if dest_check != "ok":
        raise SystemExit(f"Copy failed integrity_check: {dest_check}")
    src_counts = table_counts(src)
    dest_counts = table_counts(dest)
    if src_counts != dest_counts:
        raise SystemExit(
            f"Row counts differ.\n  live: {src_counts}\n  copy: {dest_counts}"
        )
    print(f"integrity live={src_check} copy={dest_check}")
    for table, count in src_counts.items():
        print(f"  {table}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--restore-check",
        help="Copy a backup into a separate file and verify integrity/row counts",
    )
    args = parser.parse_args()

    if args.restore_check:
        backup_path = Path(args.restore_check)
        if not backup_path.is_absolute():
            backup_path = ROOT / backup_path
        if not backup_path.exists():
            print(f"Backup not found: {backup_path}")
            return 1
        test_path = backups_dir() / "restore-check.db"
        restore_check(backup_path, test_path)
        print(f"Restore check OK: {backup_path} -> {test_path}")
        return 0

    src = Path(DB_PATH)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        print(f"No live database at {src}. Nothing to back up.")
        return 0

    dest = create_timestamped_backup()
    if dest is None:
        print(f"No live database at {src}. Nothing to back up.")
        return 0
    print(f"Backup written to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
