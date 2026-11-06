"""База даних SQLite: бізнес-підключення, клієнти, повідомлення."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

CLIENT_NEW = "новий"
CLIENT_OLD = "старий"

STATUS_INVESTOR = "інвестор"
STATUS_PARTNER = "партнер"

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
    excluded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'інвестор',
    partner_since TEXT
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
    done_text TEXT,
    replied_at TEXT,
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
    status: str = STATUS_INVESTOR
    partner_since: str | None = None


@dataclass
class Task:
    task_date: str
    chat_id: int
    position: int
    last_contact_at: str | None
    created_at: str
    done_at: str | None
    done_text: str | None = None    # перше повідомлення куратора, яким виконано задачу
    replied_at: str | None = None   # коли клієнт відповів після дотику
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
        if "status" not in cols:
            self.conn.execute(f"ALTER TABLE clients ADD COLUMN status TEXT NOT NULL DEFAULT '{STATUS_INVESTOR}'")
        if "partner_since" not in cols:
            self.conn.execute("ALTER TABLE clients ADD COLUMN partner_since TEXT")
        task_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "done_text" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN done_text TEXT")
        if "replied_at" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN replied_at TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_date ON messages(sender, date)")

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
        # status і partner_since змінюються лише через set_status — тут їх не перезаписуємо.
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
            {k: v for k, v in client.__dict__.items() if k not in ("status", "partner_since")},
        )
        self.conn.commit()

    def set_status(self, chat_id: int, status: str, now_iso: str) -> Client | None:
        """Інвестор ↔ партнер. Дата переходу ставиться при переведенні в партнери."""
        partner_since = now_iso if status == STATUS_PARTNER else None
        self.conn.execute(
            """UPDATE clients SET status=?,
                 partner_since=CASE WHEN ?='партнер' THEN COALESCE(partner_since, ?) ELSE NULL END
               WHERE chat_id=?""", (status, status, partner_since, chat_id))
        self.conn.commit()
        return self.get_client(chat_id)

    def all_clients(self, include_excluded: bool = False) -> list[Client]:
        sql = "SELECT * FROM clients" + ("" if include_excluded else " WHERE excluded=0")
        return [Client(**dict(r)) for r in self.conn.execute(sql)]

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

    def mark_tasks_done(self, chat_id: int, done_iso: str, text: str | None = None) -> list[Task]:
        """Позначає виконаними всі відкриті задачі по цьому чату. Повертає їх."""
        dates = [r[0] for r in self.conn.execute(
            "SELECT task_date FROM tasks WHERE chat_id=? AND done_at IS NULL", (chat_id,))]
        if not dates:
            return []
        self.conn.execute("UPDATE tasks SET done_at=?, done_text=? WHERE chat_id=? AND done_at IS NULL",
                          (done_iso, text, chat_id))
        self.conn.commit()
        return [t for d in dates for t in self.tasks_for(d) if t.chat_id == chat_id]

    def mark_tasks_replied(self, chat_id: int, reply_iso: str) -> list[Task]:
        """Клієнт відповів після дотику — позначаємо виконані задачі без відповіді."""
        dates = [r[0] for r in self.conn.execute(
            """SELECT task_date FROM tasks WHERE chat_id=? AND done_at IS NOT NULL AND replied_at IS NULL
               AND done_at <= ?""", (chat_id, reply_iso))]
        if not dates:
            return []
        self.conn.execute(
            """UPDATE tasks SET replied_at=? WHERE chat_id=? AND done_at IS NOT NULL AND replied_at IS NULL
               AND done_at <= ?""", (reply_iso, chat_id, reply_iso))
        self.conn.commit()
        return [t for d in dates for t in self.tasks_for(d) if t.chat_id == chat_id]

    def all_tasks(self) -> list[Task]:
        return [Task(**dict(r)) for r in self.conn.execute("SELECT * FROM tasks")]

    def curator_messages(self, sender: str, since_iso: str | None) -> list[tuple[int, str]]:
        """(chat_id, date) повідомлень куратора в чатах клієнтів (без «не клієнтів»)."""
        sql = """SELECT m.chat_id, m.date FROM messages m JOIN clients c ON c.chat_id = m.chat_id
                 WHERE m.sender=? AND c.excluded=0"""
        params: list = [sender]
        if since_iso:
            sql += " AND m.date >= ?"
            params.append(since_iso)
        return [(r[0], r[1]) for r in self.conn.execute(sql, params)]

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
            "partners": q(f"SELECT COUNT(*) FROM clients WHERE excluded=0 AND status='{STATUS_PARTNER}'"),
        }
