"""Fail CI if secrets, the live SQLite file or bytecode are tracked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


FORBIDDEN_NAMES = {".env"}
FORBIDDEN_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc", ".pyo"}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=root,
    ).split(b"\0")
    bad: list[str] = []
    for raw in tracked:
        if not raw:
            continue
        path = raw.decode("utf-8").replace("\\", "/")
        name = Path(path).name
        if name in FORBIDDEN_NAMES:
            bad.append(path)
        if any(path.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            bad.append(path)
        if "__pycache__/" in path or path.startswith("__pycache__/"):
            bad.append(path)
    if bad:
        print("Tracked files that must stay out of git:")
        for path in sorted(set(bad)):
            print(f"  {path}")
        return 1
    print("OK: .env, *.db and bytecode are not tracked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
