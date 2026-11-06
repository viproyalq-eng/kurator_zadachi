"""База даних SQLite: бізнес-підключення, клієнти, повідомлення."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

CLIENT_NEW = "новий"
CLIENT_OLD = "старий"

SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    owner_name TEXT,
    can_reply INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    chat_id INTEGER PRIMARY KEY,
    business_connection_id TEXT,
    name TEXT,
    username TEXT,
    client_type TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_message_at TEXT,
    last_from TEXT,
    welcome_sent_at TEXT,
    stage TEXT,
    excluded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    business_connection_id TEXT,
    sender TEXT NOT NULL,
    text TEXT,
    date TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, date);

CREATE TABLE IF NOT EXISTS tasks (
    task_date TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    last_contact_at TEXT,
    created_at TEXT NOT NULL,
    done_at TEXT,
    PRIMARY KEY (task_date, chat_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Client:
    chat_id: int
    business_connection_id: str | None
    name: str | None
    username: str | None
    client_type: str
    first_seen: str
    last_message_at: str | None
    last_from: str | None
    welcome_sent_at: str | None
    stage: str | None
    excluded: int = 0  # 1 = не клієнт (команда, друзі) — не потрапляє в задачі


@dataclass
class Task:
    task_date: str
    chat_id: int
    position: int
    last_contact_at: str | None
    created_at: str
    done_at: str | None
    name: str | None = None
    username: str | None = None


@dataclass
class Connection:
    id: str
    owner_user_id: int
    owner_name: str | None
    can_reply: bool
    is_enabled: bool


class Database:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        # База, створена попередньою версією бота, отримує нові стовпці.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(clients)")}
        if "excluded" not in cols:
            self.conn.execute("ALTER TABLE clients ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    # --- бізнес-підключення ---
    def save_connection(self, conn: Connection, now_iso: str) -> None:
        self.conn.execute(
            """INSERT INTO business_connections (id, owner_user_id, owner_name, can_reply, is_enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                 owner_name=excluded.owner_name, can_reply=excluded.can_reply,
                 is_enabled=excluded.is_enabled, updated_at=excluded.updated_at""",
            (conn.id, conn.owner_user_id, conn.owner_name, int(conn.can_reply), int(conn.is_enabled), now_iso),
        )
        self.conn.commit()

    def get_connection(self, connection_id: str) -> Connection | None:
        row = self.conn.execute("SELECT * FROM business_connections WHERE id=?", (connection_id,)).fetchone()
        if not row:
            return None
        return Connection(row["id"], row["owner_user_id"], row["owner_name"], bool(row["can_reply"]), bool(row["is_enabled"]))

    # --- клієнти ---
    def get_client(self, chat_id: int) -> Client | None:
        row = self.conn.execute("SELECT * FROM clients WHERE chat_id=?", (chat_id,)).fetchone()
        return Client(**dict(row)) if row else None

    def upsert_client(self, client: Client) -> None:
        self.conn.execute(
            """INSERT INTO clients (chat_id, business_connection_id, name, username, client_type, first_seen,
                                    last_message_at, last_from, welcome_sent_at, stage, excluded)
               VALUES (:chat_id, :business_connection_id, :name, :username, :client_type, :first_seen,
                       :last_message_at, :last_from, :welcome_sent_at, :stage, :excluded)
               ON CONFLICT(chat_id) DO UPDATE SET business_connection_id=excluded.business_connection_id,
                 name=excluded.name, username=excluded.username, client_type=excluded.client_type,
                 last_message_at=excluded.last_message_at, last_from=excluded.last_from,
                 welcome_sent_at=excluded.welcome_sent_at, stage=excluded.stage,
                 excluded=excluded.excluded""",
            client.__dict__,
        )
        self.conn.commit()

    # --- повідомлення ---
    def add_message(self, chat_id: int, message_id: int, connection_id: str | None,
                    sender: str, text: str, date_iso: str) -> bool:
        """Повертає False, якщо таке повідомлення вже є (дубль)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO messages (chat_id, message_id, business_connection_id, sender, text, date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, message_id, connection_id, sender, text, date_iso),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def set_excluded(self, chat_id: int, excluded: bool) -> bool:
        """Повертає True, якщо значення змінилося."""
        cur = self.conn.execute("UPDATE clients SET excluded=? WHERE chat_id=? AND excluded<>?",
                                (int(excluded), chat_id, int(excluded)))
        self.conn.commit()
        return cur.rowcount == 1

    def exclude_usernames(self, usernames: set[str]) -> None:
        if not usernames:
            return
        marks = ",".join("?" for _ in usernames)
        self.conn.execute(f"UPDATE clients SET excluded=1 WHERE lower(username) IN ({marks})",
                          [u.lower() for u in usernames])
        self.conn.commit()

    # --- задачі ---
    def task_candidates(self, cutoff_iso: str, limit: int) -> list[Client]:
        """Клієнти, з якими не було жодного повідомлення з моменту cutoff — найстаріші першими."""
        rows = self.conn.execute(
            """SELECT * FROM clients
               WHERE excluded=0 AND last_message_at IS NOT NULL AND last_message_at <= ?
               ORDER BY last_message_at ASC LIMIT ?""", (cutoff_iso, limit)).fetchall()
        return [Client(**dict(r)) for r in rows]

    def create_tasks(self, task_date: str, clients: list[Client], now_iso: str) -> None:
        for pos, c in enumerate(clients, start=1):
            self.conn.execute(
                """INSERT OR IGNORE INTO tasks (task_date, chat_id, position, last_contact_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""", (task_date, c.chat_id, pos, c.last_message_at, now_iso))
        self.conn.commit()

    def tasks_for(self, task_date: str) -> list[Task]:
        rows = self.conn.execute(
            """SELECT t.*, c.name AS name, c.username AS username FROM tasks t
               LEFT JOIN clients c ON c.chat_id = t.chat_id
               WHERE t.task_date=? ORDER BY t.position""", (task_date,)).fetchall()
        return [Task(**dict(r)) for r in rows]

    def has_tasks(self, task_date: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tasks WHERE task_date=? LIMIT 1", (task_date,)).fetchone() is not None

    def mark_tasks_done(self, chat_id: int, done_iso: str) -> list[Task]:
        """Позначає виконаними всі відкриті задачі по цьому чату. Повертає їх."""
        dates = [r[0] for r in self.conn.execute(
            "SELECT task_date FROM tasks WHERE chat_id=? AND done_at IS NULL", (chat_id,))]
        if not dates:
            return []
        self.conn.execute("UPDATE tasks SET done_at=? WHERE chat_id=? AND done_at IS NULL", (done_iso, chat_id))
        self.conn.commit()
        return [t for d in dates for t in self.tasks_for(d) if t.chat_id == chat_id]

    # --- службові налаштування ---
    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "clients": q("SELECT COUNT(*) FROM clients"),
            "new_clients": q(f"SELECT COUNT(*) FROM clients WHERE client_type='{CLIENT_NEW}'"),
            "old_clients": q(f"SELECT COUNT(*) FROM clients WHERE client_type='{CLIENT_OLD}'"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "welcomes": q("SELECT COUNT(*) FROM clients WHERE welcome_sent_at IS NOT NULL"),
            "excluded": q("SELECT COUNT(*) FROM clients WHERE excluded=1"),
        }
