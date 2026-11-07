"""Резервні копії бази: створення, зберігання, перевірка й відновлення."""
from __future__ import annotations

import io
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BACKUP_PREFIX = "kurator-backup-"
DB_NAME_IN_ZIP = "bot.db"
KEEP_LOCAL = 7
TELEGRAM_SEND_LIMIT = 50 * 1000 * 1000  # бот може надсилати файли до 50 МБ
LAST_BACKUP_KEY = "backup_last_date"


class BackupError(Exception):
    """Файл не схожий на бекап бота — пояснення для людини."""


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "clients": q("SELECT COUNT(*) FROM clients"),
        "messages": q("SELECT COUNT(*) FROM messages"),
        "tasks": q("SELECT COUNT(*) FROM tasks"),
    }


def make_backup(conn: sqlite3.Connection, out_dir: str | Path, now_utc: datetime, tz: ZoneInfo
                ) -> tuple[Path, dict[str, int]]:
    """Узгоджена копія бази (навіть під час роботи бота), стиснута в zip."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = now_utc.astimezone(tz).strftime("%Y-%m-%d_%H%M")
    tmp_db = out / f".tmp-{stamp}.db"
    if tmp_db.exists():
        tmp_db.unlink()
    dest = sqlite3.connect(tmp_db)
    try:
        conn.backup(dest)
        stats = counts(dest)
    finally:
        dest.close()
    zip_path = out / f"{BACKUP_PREFIX}{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(tmp_db, DB_NAME_IN_ZIP)
    tmp_db.unlink()
    return zip_path, stats


def rotate(out_dir: str | Path, keep: int = KEEP_LOCAL) -> list[Path]:
    """Лишає на диску лише останні `keep` копій. Повертає видалені."""
    files = sorted(Path(out_dir).glob(f"{BACKUP_PREFIX}*.zip"))
    removed = files[:-keep] if len(files) > keep else []
    for f in removed:
        f.unlink()
    return removed


def is_backup_name(name: str) -> bool:
    return name.lower().startswith(BACKUP_PREFIX) and name.lower().endswith(".zip")


def extract_backup(data: bytes, dest_path: str | Path) -> dict[str, int]:
    """Розпаковує бекап у dest_path і перевіряє, що це справді база бота. Повертає кількості."""
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
            if DB_NAME_IN_ZIP not in zf.namelist():
                raise BackupError("У файлі немає бази бота (bot.db). Надішліть файл kurator-backup-….zip від бота.")
            raw = zf.read(DB_NAME_IN_ZIP)
    except zipfile.BadZipFile as e:
        raise BackupError("Файл пошкоджений — це не zip-архів.") from e
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    try:
        conn = sqlite3.connect(dest)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("База в бекапі пошкоджена.")
            return counts(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        dest.unlink(missing_ok=True)
        raise BackupError("Файл не схожий на базу бота.") from e
    except BackupError:
        dest.unlink(missing_ok=True)
        raise


def replace_database(db_path: str | Path, new_db: str | Path) -> None:
    """Підміняє файл бази (бот має бути зупинений або з'єднання закрите)."""
    db_path = Path(db_path)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    os.replace(new_db, db_path)
