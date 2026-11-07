"""Кола обходу: усі інвестори по 20 на день; «усіх пройдено» → нове коло."""
from datetime import datetime, timedelta, timezone

from bot.config import load_config
from bot.db import CLIENT_NEW, CLIENT_OLD, Client, Connection, Database
from bot.notify import Notifier
from bot.service import BotService, IncomingMessage
from bot.tasks import format_tasks_message

VIKTOR = 777
START = datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc)   # 07:00 за Києвом


class Env:
    def __init__(self, tmp_path, per_day=2):
        self.cfg = load_config({"BOT_TOKEN": "1:A", "TASKS_PER_DAY": str(per_day)})
        self.db = Database(str(tmp_path / "bot.db"))
        self.db.save_connection(Connection("conn", VIKTOR, "Віктор", True, True), "now")
        self.group = []
        self.now = START

        async def group(text):
            self.group.append(text)

        async def nothing(*a):
            return None

        async def owner(c):
            return VIKTOR

        self.service = BotService(self.cfg, self.db, None, Notifier(None, None), nothing, owner, notify_group=group)
        self.service._now = lambda: self.now
        self.mid = 0

    def add(self, chat_id, days_ago, ctype=CLIENT_OLD):
        last = (START - timedelta(days=days_ago)).isoformat()
        self.db.upsert_client(Client(chat_id, "conn", f"К{chat_id}", None, ctype, last, last, "клієнт", None, None))

    def day(self, n):
        self.now = START + timedelta(days=n)
        return self.service.create_daily_tasks((self.now.date()).isoformat(), self.now)

    async def viktor_writes(self, chat_id, hours=2):
        self.mid += 1
        when = self.now + timedelta(hours=hours)
        self.now = when
        await self.service.handle_message(IncomingMessage("conn", chat_id, self.mid, when, VIKTOR, False,
                                                          f"К{chat_id}", None, "Добрый день!"))


async def test_full_round_then_new_round(tmp_path):
    e = Env(tmp_path, per_day=2)
    for cid, days in [(1, 300), (2, 200), (3, 100)]:
        e.add(cid, days)

    d0 = e.day(0)
    assert [t.chat_id for t in d0] == [1, 2]
    assert e.service.round_progress(e.now) == (1, 0, 3)
    await e.viktor_writes(1)
    await e.viktor_writes(2)
    assert e.service.round_progress(e.now) == (1, 2, 3) and e.group == []

    d1 = e.day(1)
    assert [t.chat_id for t in d1] == [3]          # у колі лишився один
    await e.viktor_writes(3)
    assert len(e.group) == 1 and "Усіх клієнтів пройдено" in e.group[0] and "Коло №1 завершено: 3" in e.group[0]
    assert e.service.round_progress(e.now)[:2] == (2, 0)

    # нове коло: кому писали менше 10 днів тому — пізніше
    assert e.day(2) == []
    d11 = e.day(11)
    assert [t.chat_id for t in d11] == [1, 2]      # знову від найдавнішого контакту


async def test_undone_task_returns_next_day_within_round(tmp_path):
    e = Env(tmp_path, per_day=2)
    for cid, days in [(1, 300), (2, 200), (3, 100)]:
        e.add(cid, days)
    e.day(0)
    await e.viktor_writes(2)                        # №1 Віктор не обійшов
    assert [t.chat_id for t in e.day(1)] == [1, 3]


async def test_partners_and_cold_are_not_in_round(tmp_path):
    e = Env(tmp_path, per_day=20)
    for cid in (1, 2, 3, 4):
        e.add(cid, 100)
    e.add(5, 100, CLIENT_NEW)                       # новий інвестор — теж у колі
    e.service.set_status(2, "партнер", START)
    e.service.set_cold(3, True, START)
    e.db.set_excluded(4, True)
    assert sorted(t.chat_id for t in e.day(0)) == [1, 5]
    assert e.service.round_progress(e.now) == (1, 0, 2)


async def test_round_finishes_when_last_uncovered_becomes_cold(tmp_path):
    e = Env(tmp_path, per_day=5)
    e.add(1, 100)
    e.add(2, 100)
    e.day(0)
    await e.viktor_writes(1)
    e.service.set_cold(2, True, e.now)
    assert await e.service.maybe_finish_round(e.now)
    assert "Усіх клієнтів пройдено" in e.group[-1]
    assert not await e.service.maybe_finish_round(e.now)     # нове коло ще не пройдене


async def test_empty_base_never_announces(tmp_path):
    e = Env(tmp_path)
    assert not await e.service.maybe_finish_round(START)
    assert e.group == []


def test_progress_line_in_morning_message():
    text = format_tasks_message("2026-11-07", [], START, 10, round_progress=(2, 120, 1240))
    assert "🔁 Коло №2: пройдено 120 з 1240 клієнтів (залишилось 1120)" in text
