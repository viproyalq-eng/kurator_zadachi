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
    partner_since TEXT,
    welcome_replied_at TEXT,
    reminder_sent_at TEXT,
    cold_since TEXT,
    stage_at TEXT
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
    kind TEXT NOT NULL DEFAULT 'stale',
    note TEXT,
    PRIMARY KEY (task_date, chat_id)
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    business_connection_id TEXT,
    kind TEXT NOT NULL,            -- welcome | reminder
    due_at TEXT NOT NULL,          -- коли надіслати (UTC)
    deadline_at TEXT NOT NULL,     -- до коли Telegram дозволяє написати (24 год після клієнта)
    status TEXT NOT NULL,          -- pending | sent | cancelled | missed | tasked
    created_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sched_status ON scheduled_messages(status, due_at);

CREATE TABLE IF NOT EXISTS client_stages (
    chat_id INTEGER NOT NULL,
    stage TEXT NOT NULL,        -- bonus | t1 … t11 | lost:t3 | lost_task:t3
    at TEXT NOT NULL,           -- коли вперше (UTC)
    PRIMARY KEY (chat_id, stage)
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
    welcome_replied_at: str | None = None   # клієнт відповів після привітання
    reminder_sent_at: str | None = None     # бот надіслав нагадування
    cold_since: str | None = None           # «холодний»: без задач і без повідомлень бота
    stage_at: str | None = None             # коли клієнт перейшов на поточний етап


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
    kind: str = "stale"             # stale — давно не спілкувалися; missed; no_reply
    note: str | None = None
    name: str | None = None
    username: str | None = None


@dataclass
class Scheduled:
    id: int
    chat_id: int
    business_connection_id: str | None
    kind: str
    due_at: str
    deadline_at: str
    status: str
    created_at: str
    sent_at: str | None = None
    error: str | None = None


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
        for col in ("partner_since", "welcome_replied_at", "reminder_sent_at", "cold_since", "stage_at"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT")
        task_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "done_text" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN done_text TEXT")
        if "replied_at" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN replied_at TEXT")
        if "kind" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'stale'")
        if "note" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN note TEXT")
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
        # status, partner_since і cold_since змінюються лише окремими командами — тут їх не перезаписуємо.
        self.conn.execute(
            """INSERT INTO clients (chat_id, business_connection_id, name, username, client_type, first_seen,
                                    last_message_at, last_from, welcome_sent_at, stage, excluded,
                                    welcome_replied_at, reminder_sent_at, stage_at)
               VALUES (:chat_id, :business_connection_id, :name, :username, :client_type, :first_seen,
                       :last_message_at, :last_from, :welcome_sent_at, :stage, :excluded,
                       :welcome_replied_at, :reminder_sent_at, :stage_at)
               ON CONFLICT(chat_id) DO UPDATE SET business_connection_id=excluded.business_connection_id,
                 name=excluded.name, username=excluded.username, client_type=excluded.client_type,
                 first_seen=MIN(clients.first_seen, excluded.first_seen),
                 last_message_at=excluded.last_message_at, last_from=excluded.last_from,
                 welcome_sent_at=excluded.welcome_sent_at, stage=excluded.stage,
                 excluded=excluded.excluded, welcome_replied_at=excluded.welcome_replied_at,
                 reminder_sent_at=excluded.reminder_sent_at, stage_at=excluded.stage_at""",
            {k: v for k, v in client.__dict__.items() if k not in ("status", "partner_since", "cold_since")},
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

    def set_cold(self, chat_id: int, cold_iso: str | None) -> Client | None:
        """cold_iso — дата переносу в холодні; None — повернути з холодних."""
        self.conn.execute("UPDATE clients SET cold_since=? WHERE chat_id=?", (cold_iso, chat_id))
        self.conn.commit()
        return self.get_client(chat_id)

    def cold_clients(self) -> list[Client]:
        rows = self.conn.execute("SELECT * FROM clients WHERE cold_since IS NOT NULL ORDER BY cold_since DESC")
        return [Client(**dict(r)) for r in rows]

    def partner_clients(self) -> list[Client]:
        rows = self.conn.execute(
            f"SELECT * FROM clients WHERE status='{STATUS_PARTNER}' AND excluded=0 ORDER BY partner_since DESC")
        return [Client(**dict(r)) for r in rows]

    def find_by_username(self, username: str) -> Client | None:
        row = self.conn.execute("SELECT * FROM clients WHERE lower(username)=?",
                                (username.lstrip("@").lower(),)).fetchone()
        return Client(**dict(row)) if row else None

    def client_messages_after(self, chat_id: int, client_sender: str, after_iso: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND sender=? AND date > ?",
                                 (chat_id, client_sender, after_iso)).fetchone()[0]

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
    # --- кола обходу: інвестори (нові й старі), без партнерів, холодних і «не клієнтів» ---
    _ROUND_POOL = f"c.excluded=0 AND c.cold_since IS NULL AND c.status <> '{STATUS_PARTNER}'"
    _COVERED = "EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=c.chat_id AND m.sender=? AND m.date > ?)"

    def round_candidates(self, cutoff_iso: str, round_start_iso: str, curator: str, limit: int) -> list[Client]:
        """Ще не пройдені в цьому колі (Віктор їм не писав), без контакту з cutoff — найстаріші першими."""
        rows = self.conn.execute(
            f"""SELECT * FROM clients c WHERE {self._ROUND_POOL} AND NOT {self._COVERED}
                AND c.last_message_at IS NOT NULL AND c.last_message_at <= ?
                ORDER BY c.last_message_at ASC LIMIT ?""", (curator, round_start_iso, cutoff_iso, limit))
        return [Client(**dict(r)) for r in rows]

    def round_progress(self, round_start_iso: str, curator: str) -> tuple[int, int]:
        """(пройдено в цьому колі, усього в колі)."""
        total = self.conn.execute(f"SELECT COUNT(*) FROM clients c WHERE {self._ROUND_POOL}").fetchone()[0]
        covered = self.conn.execute(f"SELECT COUNT(*) FROM clients c WHERE {self._ROUND_POOL} AND {self._COVERED}",
                                    (curator, round_start_iso)).fetchone()[0]
        return covered, total

    def task_candidates(self, cutoff_iso: str, limit: int) -> list[Client]:
        """Клієнти, з якими не було жодного повідомлення з моменту cutoff — найстаріші першими."""
        rows = self.conn.execute(
            """SELECT * FROM clients
               WHERE excluded=0 AND cold_since IS NULL AND last_message_at IS NOT NULL AND last_message_at <= ?
               ORDER BY last_message_at ASC LIMIT ?""", (cutoff_iso, limit)).fetchall()
        return [Client(**dict(r)) for r in rows]

    def create_tasks(self, task_date: str, clients: list[Client], now_iso: str,
                     kind: str = "stale", notes: dict[int, str] | None = None) -> None:
        start = self.conn.execute("SELECT COALESCE(MAX(position), 0) FROM tasks WHERE task_date=?",
                                  (task_date,)).fetchone()[0]
        for pos, c in enumerate(clients, start=start + 1):
            self.conn.execute(
                """INSERT OR IGNORE INTO tasks (task_date, chat_id, position, last_contact_at, created_at, kind, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_date, c.chat_id, pos, c.last_message_at, now_iso, kind, (notes or {}).get(c.chat_id)))
        self.conn.commit()

    def has_task_kind(self, chat_id: int, kind: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tasks WHERE chat_id=? AND kind=? LIMIT 1",
                                 (chat_id, kind)).fetchone() is not None

    # --- заплановані повідомлення клієнтам ---
    def schedule_message(self, chat_id: int, connection_id: str | None, kind: str, due_iso: str,
                         deadline_iso: str, now_iso: str, status: str = "pending") -> int:
        """Нове заплановане повідомлення; попереднє незавершене такого ж типу скасовується."""
        self.conn.execute("UPDATE scheduled_messages SET status='cancelled' "
                          "WHERE chat_id=? AND kind=? AND status='pending'", (chat_id, kind))
        cur = self.conn.execute(
            """INSERT INTO scheduled_messages (chat_id, business_connection_id, kind, due_at, deadline_at,
                                               status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, connection_id, kind, due_iso, deadline_iso, status, now_iso))
        self.conn.commit()
        return cur.lastrowid

    def scheduled(self, chat_id: int | None = None, status: str | None = None) -> list[Scheduled]:
        sql, params = "SELECT * FROM scheduled_messages WHERE 1=1", []
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        return [Scheduled(**dict(r)) for r in self.conn.execute(sql + " ORDER BY id", params)]

    def due_messages(self, now_iso: str) -> list[Scheduled]:
        rows = self.conn.execute("SELECT * FROM scheduled_messages WHERE status='pending' AND due_at <= ? "
                                 "ORDER BY due_at", (now_iso,))
        return [Scheduled(**dict(r)) for r in rows]

    def update_scheduled(self, sched_id: int, *, status: str, due_iso: str | None = None,
                         sent_iso: str | None = None, error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE scheduled_messages SET status=?, due_at=COALESCE(?, due_at), sent_at=COALESCE(?, sent_at),
                 error=COALESCE(?, error) WHERE id=?""", (status, due_iso, sent_iso, error, sched_id))
        self.conn.commit()

    def cancel_scheduled(self, chat_id: int, kinds: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in kinds)
        cur = self.conn.execute(
            f"UPDATE scheduled_messages SET status='cancelled' WHERE chat_id=? AND status='pending' "
            f"AND kind IN ({marks})", (chat_id, *kinds))
        self.conn.commit()
        return cur.rowcount

    def extend_deadlines(self, chat_id: int, deadline_iso: str) -> None:
        """Клієнт написав ще — Telegram знову дає 24 години."""
        self.conn.execute("UPDATE scheduled_messages SET deadline_at=? WHERE chat_id=? AND status='pending' "
                          "AND deadline_at < ?", (deadline_iso, chat_id, deadline_iso))
        self.conn.commit()

    def last_client_message_at(self, chat_id: int, client_sender: str) -> str | None:
        row = self.conn.execute("SELECT MAX(date) FROM messages WHERE chat_id=? AND sender=?",
                                (chat_id, client_sender)).fetchone()
        return row[0] if row else None

    def curator_wrote_after(self, chat_id: int, curator_sender: str, after_iso: str) -> bool:
        return self.conn.execute("SELECT 1 FROM messages WHERE chat_id=? AND sender=? AND date > ? LIMIT 1",
                                 (chat_id, curator_sender, after_iso)).fetchone() is not None

    def silent_after_reminder(self, sent_before_iso: str) -> list[Client]:
        """Клієнти, яким надіслано нагадування (до вказаного часу), але вони так і не відповіли."""
        rows = self.conn.execute(
            """SELECT * FROM clients WHERE excluded=0 AND cold_since IS NULL AND reminder_sent_at IS NOT NULL
               AND reminder_sent_at < ? AND welcome_replied_at IS NULL""", (sent_before_iso,))
        return [Client(**dict(r)) for r in rows]

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

    def tasks_by_state(self, done: bool) -> list[Task]:
        """Невиконані (свіжі дні першими) або виконані (останні виконані першими) — з іменами клієнтів."""
        if done:
            where, order = "t.done_at IS NOT NULL", "t.done_at DESC"
        else:
            where, order = "t.done_at IS NULL", "t.task_date DESC, t.position ASC"
        rows = self.conn.execute(
            f"""SELECT t.*, c.name AS name, c.username AS username FROM tasks t
                LEFT JOIN clients c ON c.chat_id = t.chat_id WHERE {where} ORDER BY {order}""")
        return [Task(**dict(r)) for r in rows]

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

    def add_messages_bulk(self, rows: list[tuple]) -> int:
        """rows: (chat_id, message_id, connection_id, sender, text, date_iso). Повертає, скільки додано нових.
        Якщо повідомлення вже є — оновлюємо текст і автора (повторний імпорт виправляє попередній)."""
        before = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.conn.executemany(
            """INSERT INTO messages (chat_id, message_id, business_connection_id, sender, text, date)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, message_id) DO UPDATE SET text=excluded.text, sender=excluded.sender
               WHERE messages.text IS NOT excluded.text OR messages.sender IS NOT excluded.sender""", rows)
        self.conn.commit()
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] - before

    def live_messages(self, since_iso: str | None) -> list[tuple[int, str, str, str]]:
        """(chat_id, sender, date, text) — повідомлення від запуску бота, по чатах і в часі."""
        sql = "SELECT chat_id, sender, date, text FROM messages"
        params: list = []
        if since_iso:
            sql += " WHERE date >= ?"
            params.append(since_iso)
        return [tuple(r) for r in self.conn.execute(sql + " ORDER BY chat_id, date, id", params)]

    def alive_clients(self, client_sender: str, since_iso: str) -> int:
        return self.conn.execute(
            """SELECT COUNT(DISTINCT m.chat_id) FROM messages m JOIN clients c ON c.chat_id = m.chat_id
               WHERE c.excluded=0 AND m.sender=? AND m.date >= ?""", (client_sender, since_iso)).fetchone()[0]

    # --- етапи курсу ---
    def record_stage(self, chat_id: int, stage: str, at_iso: str) -> bool:
        """Запам'ятовує етап (найранішу дату). Повертає True, якщо етап новий."""
        existed = self.conn.execute("SELECT 1 FROM client_stages WHERE chat_id=? AND stage=?",
                                    (chat_id, stage)).fetchone() is not None
        self.conn.execute(
            """INSERT INTO client_stages (chat_id, stage, at) VALUES (?, ?, ?)
               ON CONFLICT(chat_id, stage) DO UPDATE SET at=MIN(at, excluded.at)""", (chat_id, stage, at_iso))
        self.conn.commit()
        return not existed

    def record_stage_if_missing(self, chat_id: int, stage: str, at_iso: str) -> bool:
        """Лише якщо етапу ще немає (для ✅ у списку тем: точну дату дає підпис «Тема N», якщо він є)."""
        cur = self.conn.execute("INSERT OR IGNORE INTO client_stages (chat_id, stage, at) VALUES (?, ?, ?)",
                                (chat_id, stage, at_iso))
        self.conn.commit()
        return cur.rowcount == 1

    def update_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.conn.execute("UPDATE messages SET text=? WHERE chat_id=? AND message_id=?", (text, chat_id, message_id))
        self.conn.commit()

    def stages_of(self, chat_id: int) -> dict[str, str]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT stage, at FROM client_stages WHERE chat_id=?", (chat_id,))}

    def all_stages(self) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for chat_id, stage, at in self.conn.execute("SELECT chat_id, stage, at FROM client_stages"):
            out.setdefault(chat_id, {})[stage] = at
        return out

    def last_message_at_by(self, chat_id: int, senders: tuple[str, ...]) -> str | None:
        marks = ",".join("?" for _ in senders)
        return self.conn.execute(f"SELECT MAX(date) FROM messages WHERE chat_id=? AND sender IN ({marks})",
                                 (chat_id, *senders)).fetchone()[0]

    def curator_texts(self, curator: str) -> list[tuple[int, str, str]]:
        """(chat_id, date, text) повідомлень куратора з посиланнями/текстом — для пошуку тем в історії."""
        return [tuple(r) for r in self.conn.execute(
            "SELECT chat_id, date, text FROM messages WHERE sender=? AND text IS NOT NULL ORDER BY date", (curator,))]

    def owner_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT owner_user_id FROM business_connections")}

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
            "cold": q("SELECT COUNT(*) FROM clients WHERE excluded=0 AND cold_since IS NOT NULL"),
        }
