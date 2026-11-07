from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.config import load_config
from bot.db import CLIENT_OLD, Client, Database, Task
from bot.notify import Notifier
from bot.service import BotService, IncomingMessage
from bot.tasks import format_tasks_message, is_due, task_row

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)  # 07:00 за Києвом
TODAY = "2026-11-06"


def kyiv(h, m=0, day=6):
    return datetime(2026, 11, day, h, m, tzinfo=KYIV)


def test_is_due():
    assert not is_due(kyiv(6, 59), time(7), None)
    assert is_due(kyiv(7, 0), time(7), None)
    assert is_due(kyiv(13, 0), time(7), "2026-11-05")   # бот лежав о 7:00 — надішле пізніше
    assert not is_due(kyiv(7, 1), time(7), TODAY)        # сьогодні вже надіслано
    assert is_due(kyiv(7, 0, day=7), time(7), TODAY)     # наступного дня знову


class Sheets:
    def __init__(self):
        self.tasks = {}

    def queue_message(self, row):
        pass

    def queue_client(self, chat_id, row, new_row=None):
        pass

    def queue_upsert(self, tab, row, new_row=None):
        self.tasks[(row[0], row[3])] = row


def make(tmp_path, **env):
    cfg = load_config({"BOT_TOKEN": "1:A", **env})
    db = Database(str(tmp_path / "bot.db"))
    sheets = Sheets()

    async def nothing(*a):
        return None

    service = BotService(cfg, db, sheets, Notifier(None, None), send=nothing, resolve_owner=nothing)
    return cfg, db, sheets, service


def add_client(db, chat_id, days_ago, username=None, excluded=0):
    last = (NOW - timedelta(days=days_ago)).isoformat()
    db.upsert_client(Client(chat_id, "conn", f"Клієнт {chat_id}", username, CLIENT_OLD,
                            last, last, "клієнт", None, None, excluded))


def test_picks_oldest_first_with_limits(tmp_path):
    cfg, db, sheets, service = make(tmp_path, TASKS_PER_DAY="3")
    for chat_id, days in [(1, 400), (2, 11), (3, 10), (4, 9), (5, 30), (6, 200)]:
        add_client(db, chat_id, days)
    add_client(db, 7, 500, excluded=1)               # «Не клієнт»
    add_client(db, 8, 600, username="VladBazil")     # команда
    tasks = service.create_daily_tasks(TODAY, NOW)
    assert [t.chat_id for t in tasks] == [1, 6, 5]
    assert len(sheets.tasks) == 3


def test_ten_days_boundary(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    add_client(db, 1, 10)
    add_client(db, 2, 9)
    assert [t.chat_id for t in service.create_daily_tasks(TODAY, NOW)] == [1]


def test_same_day_is_not_regenerated(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    add_client(db, 1, 50)
    first = service.create_daily_tasks(TODAY, NOW)
    add_client(db, 2, 99)
    second = service.create_daily_tasks(TODAY, NOW)
    assert [t.chat_id for t in first] == [t.chat_id for t in second] == [1]


async def test_curator_message_completes_task(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    from bot.db import Connection
    db.save_connection(Connection("conn", 777, "Віктор", True, True), "now")
    add_client(db, 1, 50)
    service.create_daily_tasks(TODAY, NOW)

    async def msg(from_user, message_id):
        await service.handle_message(IncomingMessage("conn", 1, message_id, NOW + timedelta(hours=1),
                                                     from_user, False, "Клієнт 1", None, "привіт"))

    await msg(1, 1)  # клієнт сам написав — задача ще не виконана
    assert db.tasks_for(TODAY)[0].done_at is None
    await msg(777, 2)  # Віктор написав
    assert db.tasks_for(TODAY)[0].done_at is not None
    assert sheets.tasks[("06.11.2026", "1")][6] != ""
    # після контакту клієнт не потрапить у задачі найближчі 10 днів
    tomorrow = NOW + timedelta(days=1)
    assert service.create_daily_tasks("2026-11-07", tomorrow) == []


def test_team_member_is_excluded_on_first_message(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    import asyncio
    asyncio.run(service.handle_message(IncomingMessage(
        "conn", 9, 1, NOW, 9, False, "Антон", "antoniohoshko", "привіт")))
    assert db.get_client(9).excluded == 1


def test_flags_from_sheet(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    add_client(db, 1, 50)
    add_client(db, 2, 50, username="vladbazil")
    assert service.apply_not_client_flags({1: True, 2: False, 999: True}) == 2
    assert db.get_client(1).excluded == 1
    assert db.get_client(2).excluded == 1   # команда лишається виключеною
    assert service.apply_not_client_flags({1: False}) == 1
    assert db.get_client(1).excluded == 0


def test_message_format():
    tasks = [
        Task(TODAY, 1, 1, (NOW - timedelta(days=45)).isoformat(), NOW.isoformat(), None,
             name="Іван <Петренко>", username="ivan"),
        Task(TODAY, 2, 2, (NOW - timedelta(days=12)).isoformat(), NOW.isoformat(), NOW.isoformat()),
    ]
    text = format_tasks_message(TODAY, tasks, NOW, 10, yesterday=(15, 20))
    assert "Задачі на 06.11.2026" in text
    assert "Вчора виконано: 15 з 20" in text
    assert "1. Іван &lt;Петренко&gt; · @ivan — останній контакт 22.09.2026 (45 дн. тому)" in text
    # без username — посилання на чат у веб-версії Telegram
    assert '2. ✅ Без імені · <a href="https://web.telegram.org/a/#2">відкрити чат</a>' in text
    assert "tg://user" not in text
    assert "Виконано: 1 з 2" in text


def test_empty_message():
    assert "немає клієнтів" in format_tasks_message(TODAY, [], NOW, 10)


def test_task_row_days_at_creation():
    t = Task(TODAY, 1, 1, (NOW - timedelta(days=45)).isoformat(), NOW.isoformat(),
             (NOW + timedelta(days=3)).isoformat(), done_text="Добрый день! Как дела?",
             replied_at=(NOW + timedelta(days=4)).isoformat(), name="Іван", username="ivan")
    row = task_row(t, KYIV)
    assert row[0] == "06.11.2026" and row[3] == "1" and row[5] == "45"
    assert row[6] == "09.11.2026 07:00"
    assert row[7] == "Добрый день! Как дела?" and row[8] == "10.11.2026 07:00"
    assert row[10] == "https://t.me/ivan"


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE clients (chat_id INTEGER PRIMARY KEY, business_connection_id TEXT, name TEXT,
                   username TEXT, client_type TEXT NOT NULL, first_seen TEXT NOT NULL, last_message_at TEXT,
                   last_from TEXT, welcome_sent_at TEXT, stage TEXT)""")
    con.execute("INSERT INTO clients VALUES (1,'c','Іван',NULL,'старий','2026-01-01T00:00:00+00:00',"
                "'2026-01-01T00:00:00+00:00','клієнт',NULL,NULL)")
    con.commit()
    con.close()
    db = Database(path)
    assert db.get_client(1).excluded == 0
    assert len(db.task_candidates(NOW.isoformat(), 20)) == 1


# --- партнери, пошук, перевірка дотиків ---

def test_find_clients(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    for chat_id, name, user, days in [(1, "Іван Петренко", "ivan_p", 5), (2, "Іванна", None, 1),
                                      (3, "Олег", "oleg", 2)]:
        last = (NOW - timedelta(days=days)).isoformat()
        db.upsert_client(Client(chat_id, "c", name, user, CLIENT_OLD, last, last, "клієнт", None, None))
    assert [c.chat_id for c in service.find_clients("іван")] == [2, 1]      # без різниці у великих літерах
    assert [c.chat_id for c in service.find_clients("@OLEG")] == [3]
    assert [c.chat_id for c in service.find_clients("3")] == [3]
    assert service.find_clients("   ") == []
    add_client(db, 9, 1, username="vladbazil")
    db.exclude_usernames(cfg.team_usernames)
    assert service.find_clients("vladbazil") == []                         # команду не пропонуємо


def test_set_status(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    add_client(db, 1, 5)
    client, changed = service.set_status(1, "партнер", NOW)
    assert changed and client.status == "партнер" and client.partner_since == NOW.isoformat()
    client, changed = service.set_status(1, "партнер", NOW + timedelta(days=1))
    assert not changed and client.partner_since == NOW.isoformat()          # дата першого переведення
    client, changed = service.set_status(1, "інвестор", NOW)
    assert changed and client.partner_since is None
    assert service.set_status(999, "партнер", NOW) == (None, False)


async def test_status_survives_new_messages(tmp_path):
    cfg, db, sheets, service = make(tmp_path)
    add_client(db, 1, 5)
    service.set_status(1, "партнер", NOW)
    await service.handle_message(IncomingMessage("conn", 1, 1, NOW, 1, False, "Клієнт 1", None, "привіт"))
    assert db.get_client(1).status == "партнер"


async def test_touch_text_and_reply_are_tracked(tmp_path):
    from bot.db import Connection
    cfg, db, sheets, service = make(tmp_path)
    db.save_connection(Connection("conn", 777, "Віктор", True, True), "now")
    add_client(db, 1, 50)
    service.create_daily_tasks(TODAY, NOW)

    async def msg(from_user, message_id, text, hours):
        await service.handle_message(IncomingMessage("conn", 1, message_id, NOW + timedelta(hours=hours),
                                                     from_user, False, "Клієнт 1", None, text))

    await msg(777, 1, "Добрый день! Как успехи с роботом?", 1)
    await msg(777, 2, "ще одне", 2)                       # текст дотику — перше повідомлення
    task = db.tasks_for(TODAY)[0]
    assert task.done_text == "Добрый день! Как успехи с роботом?" and task.replied_at is None
    await msg(1, 3, "Все отлично", 5)
    task = db.tasks_for(TODAY)[0]
    assert task.replied_at is not None
    row = sheets.tasks[("06.11.2026", "1")]
    assert row[7] == "Добрый день! Как успехи с роботом?" and row[8] != ""


def test_previous_version_database_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "v2.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE clients (chat_id INTEGER PRIMARY KEY, business_connection_id TEXT, name TEXT, username TEXT,
            client_type TEXT NOT NULL, first_seen TEXT NOT NULL, last_message_at TEXT, last_from TEXT,
            welcome_sent_at TEXT, stage TEXT, excluded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tasks (task_date TEXT NOT NULL, chat_id INTEGER NOT NULL, position INTEGER NOT NULL,
            last_contact_at TEXT, created_at TEXT NOT NULL, done_at TEXT, PRIMARY KEY (task_date, chat_id));
        INSERT INTO clients VALUES (1,'c','Іван',NULL,'старий','2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00','клієнт',NULL,NULL,0);
        INSERT INTO tasks VALUES ('2026-11-06',1,1,NULL,'2026-11-06T05:00:00+00:00',NULL);
    """)
    con.commit()
    con.close()
    db = Database(path)
    assert db.get_client(1).status == "інвестор"
    assert db.tasks_for("2026-11-06")[0].done_text is None
