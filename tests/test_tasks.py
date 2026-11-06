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
        Task(TODAY, 1, 1, (NOW - timedelta(days=45)).isoformat(), NOW.isoformat(), None, "Іван <Петренко>", "ivan"),
        Task(TODAY, 2, 2, (NOW - timedelta(days=12)).isoformat(), NOW.isoformat(), NOW.isoformat(), None, None),
    ]
    text = format_tasks_message(TODAY, tasks, NOW, 10, yesterday=(15, 20))
    assert "Задачі на 06.11.2026" in text
    assert "Вчора виконано: 15 з 20" in text
    assert '<a href="tg://user?id=1">Іван &lt;Петренко&gt;</a> (@ivan) — 45 дн. тому' in text
    assert "2. ✅ " in text and "Без імені" in text
    assert "Виконано: 1 з 2" in text


def test_empty_message():
    assert "немає клієнтів" in format_tasks_message(TODAY, [], NOW, 10)


def test_task_row_days_at_creation():
    t = Task(TODAY, 1, 1, (NOW - timedelta(days=45)).isoformat(), NOW.isoformat(),
             (NOW + timedelta(days=3)).isoformat(), "Іван", "ivan")
    row = task_row(t, KYIV)
    assert row[0] == "06.11.2026" and row[3] == "1" and row[5] == "45"
    assert row[6] == "09.11.2026 07:00"


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
