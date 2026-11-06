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
    stage TEXT
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
        self.conn.commit()

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
                                    last_message_at, last_from, welcome_sent_at, stage)
               VALUES (:chat_id, :business_connection_id, :name, :username, :client_type, :first_seen,
                       :last_message_at, :last_from, :welcome_sent_at, :stage)
               ON CONFLICT(chat_id) DO UPDATE SET business_connection_id=excluded.business_connection_id,
                 name=excluded.name, username=excluded.username, client_type=excluded.client_type,
                 last_message_at=excluded.last_message_at, last_from=excluded.last_from,
                 welcome_sent_at=excluded.welcome_sent_at, stage=excluded.stage""",
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

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "clients": q("SELECT COUNT(*) FROM clients"),
            "new_clients": q(f"SELECT COUNT(*) FROM clients WHERE client_type='{CLIENT_NEW}'"),
            "old_clients": q(f"SELECT COUNT(*) FROM clients WHERE client_type='{CLIENT_OLD}'"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "welcomes": q("SELECT COUNT(*) FROM clients WHERE welcome_sent_at IS NOT NULL"),
        }
