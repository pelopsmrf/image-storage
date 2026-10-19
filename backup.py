"""Резервное копирование базы images_db через pg_dump в контейнере PostgreSQL.

    python backup.py                       # создать backups/backup_<дата>_<время>.sql
    python backup.py --keep 7              # создать копию и оставить только 7 последних
    python backup.py --restore backups/backup_2025-01-24_153000.sql
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BACKUP_DIR = PROJECT_DIR / "backups"
LOG_FILE = PROJECT_DIR / "logs" / "app.log"
CONTAINER = "postgres_container"
DB_NAME = "images_db"
DB_USER = "postgres"

logger = logging.getLogger("image_storage.backup")


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)


def create_backup() -> Path | None:
    BACKUP_DIR.mkdir(exist_ok=True)
    destination = BACKUP_DIR / f"backup_{datetime.now():%Y-%m-%d_%H%M%S}.sql"
    # --clean --if-exists: при восстановлении таблица пересоздаётся, а не дублируется
    command = ["docker", "exec", CONTAINER, "pg_dump", "-U", DB_USER, "--clean", "--if-exists", DB_NAME]

    try:
        with destination.open("wb") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE)
    except OSError as error:
        destination.unlink(missing_ok=True)
        logger.error("Ошибка: резервное копирование не удалось (%s).", error)
        return None

    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        reason = result.stderr.decode("utf-8", errors="replace").strip() or f"код {result.returncode}"
        logger.error("Ошибка: резервное копирование не удалось (%s).", reason)
        return None

    logger.info(
        "Успех: создана резервная копия %s (%.1f КБ).",
        destination.name,
        destination.stat().st_size / 1024,
    )
    return destination


def remove_old_backups(keep: int) -> None:
    backups = sorted(BACKUP_DIR.glob("backup_*.sql"), reverse=True)
    for old_backup in backups[keep:]:
        old_backup.unlink()
        logger.info("Успех: удалена старая резервная копия %s.", old_backup.name)


def restore_backup(source: Path) -> bool:
    if not source.is_file():
        logger.error("Ошибка: файл резервной копии %s не найден.", source)
        return False

    command = ["docker", "exec", "-i", CONTAINER, "psql", "-U", DB_USER, "-v", "ON_ERROR_STOP=1", DB_NAME]
    try:
        with source.open("rb") as dump:
            result = subprocess.run(command, stdin=dump, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as error:
        logger.error("Ошибка: восстановление из %s не удалось (%s).", source.name, error)
        return False

    if result.returncode != 0:
        reason = result.stderr.decode("utf-8", errors="replace").strip() or f"код {result.returncode}"
        logger.error("Ошибка: восстановление из %s не удалось (%s).", source.name, reason)
        return False

    logger.info("Успех: база данных восстановлена из %s.", source.name)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Резервное копирование базы images_db")
    parser.add_argument("--restore", type=Path, metavar="FILE", help="восстановить базу из резервной копии")
    parser.add_argument("--keep", type=int, metavar="N", help="оставить только N последних резервных копий")
    args = parser.parse_args()
    setup_logging()

    if args.restore:
        return 0 if restore_backup(args.restore) else 1

    if create_backup() is None:
        return 1
    if args.keep is not None and args.keep > 0:
        remove_old_backups(args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
