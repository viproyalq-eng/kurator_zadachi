"""Сценарії: привітання → відповідь / нагадування через 2 години, лише з 7:00 до 20:30 за Києвом."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.config import load_config
from bot.db import Connection, Database
from bot.notify import Notifier
from bot.schedule import in_send_window, next_send_time, reply_deadline
from bot.service import BotService, IncomingMessage

KYIV = ZoneInfo("Europe/Kyiv")
OWNER = 777
CLIENT = 100


def at(day, h, m=0):
    """Час за Києвом (листопад 2026) → UTC."""
    return datetime(2026, 11, day, h, m, tzinfo=KYIV).astimezone(timezone.utc)


# --- правило часу ---

@pytest.mark.parametrize("local,expected", [
    ((6, 12, 0), (6, 12, 0)),     # робочий час — одразу
    ((6, 6, 59), (6, 7, 0)),      # рано — о 7:00
    ((6, 20, 29), (6, 20, 29)),
    ((6, 20, 30), (7, 7, 0)),     # 20:30 і пізніше — завтра о 7:00
    ((6, 23, 0), (7, 7, 0)),
])
def test_next_send_time(local, expected):
    got = next_send_time(at(*local), KYIV, time(7), time(20, 30))
    assert got == at(*expected)


def test_window_and_deadline():
    assert in_send_window(at(6, 7), KYIV, time(7), time(20, 30))
    assert not in_send_window(at(6, 20, 30), KYIV, time(7), time(20, 30))
    assert reply_deadline(at(6, 12)) == at(7, 11, 55)


# --- середовище ---

class Env:
    def __init__(self, tmp_path, send_fails=False):
        self.cfg = load_config({"BOT_TOKEN": "1:A", "TASKS_CHAT_ID": "-1001"})
        self.db = Database(str(tmp_path / "bot.db"))
        self.db.save_connection(Connection("conn", OWNER, "Віктор", True, True), "now")
        self.sent, self.group, self.admin = [], [], []
        self.now = at(6, 12)
        self.send_fails = send_fails
        self.next_id = 1000

        async def send(conn_id, chat_id, text):
            if self.send_fails:
                raise RuntimeError("BUSINESS_PEER_USAGE_MISSING")
            self.next_id += 1
            self.sent.append((self.now, text))
            return self.next_id, self.now

        async def group(text):
            self.group.append(text)

        async def admin(chat_id, text):
            self.admin.append(text)

        async def owner(conn_id):
            return OWNER

        self.service = BotService(self.cfg, self.db, None, Notifier(1, admin), send, owner, notify_group=group)
        self.service._now = lambda: self.now
        self.msg_id = 0

    async def client_says(self, text, when):
        self.now = when
        self.msg_id += 1
        await self.service.handle_message(IncomingMessage("conn", CLIENT, self.msg_id, when, CLIENT, False,
                                                          "Іван", "ivan", text))

    async def viktor_says(self, text, when):
        self.now = when
        self.msg_id += 1
        await self.service.handle_message(IncomingMessage("conn", CLIENT, self.msg_id, when, OWNER, False,
                                                          "Іван", "ivan", text))

    async def tick(self, when):
        self.now = when
        await self.service.process_scheduled(when)

    def texts(self):
        return [t for _, t in self.sent]

    @property
    def client(self):
        return self.db.get_client(CLIENT)


WELCOME_START = "Приветствую👋"
REMINDER_START = "Приветствую😊"


async def test_daytime_no_reply_gets_reminder_after_two_hours(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    assert e.texts()[0].startswith(WELCOME_START)
    await e.tick(at(6, 13, 59))
    assert len(e.sent) == 1
    await e.tick(at(6, 14, 0))
    assert e.texts()[1].startswith(REMINDER_START) and "Курс Инвестора" in e.texts()[1]
    assert e.client.stage == "нагадування надіслано"
    await e.tick(at(6, 18))
    assert len(e.sent) == 2  # нагадування лише одне


async def test_reply_notifies_group_and_cancels_reminder(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.client_says("Я з Києва, раніше не інвестував", at(6, 12, 40))
    assert len(e.group) == 1
    assert "відповів на привітання" in e.group[0] and "ваша черга" in e.group[0]
    assert "Я з Києва, раніше не інвестував" in e.group[0] and "Іван · @ivan" in e.group[0]
    assert e.client.stage == "відповів на привітання"
    await e.tick(at(6, 14, 1))
    assert len(e.sent) == 1  # нагадування скасовано
    await e.client_says("ще питання", at(6, 15))
    assert len(e.group) == 1  # «ваша черга» — лише на першу відповідь


async def test_evening_bonus_reminder_moves_to_7am(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 19, 30))
    assert len(e.sent) == 1  # привітання одразу — ще робочий час
    await e.tick(at(6, 21, 30))
    await e.tick(at(7, 6, 59))
    assert len(e.sent) == 1
    await e.tick(at(7, 7, 0))
    assert e.texts()[1].startswith(REMINDER_START)
    assert e.sent[1][0] == at(7, 7, 0)


async def test_night_bonus_welcome_waits_for_7am(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 23, 0))
    assert e.sent == []
    assert e.client.stage == "привітання — чекає 7:00"
    await e.client_says("алло?", at(6, 23, 10))   # це ще не відповідь на привітання
    assert e.group == []
    await e.tick(at(7, 6, 59))
    assert e.sent == []
    await e.tick(at(7, 7, 0))
    assert e.texts() and e.texts()[0].startswith(WELCOME_START)
    await e.tick(at(7, 9, 0))
    assert e.texts()[1].startswith(REMINDER_START)


async def test_viktor_writing_cancels_reminder(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.viktor_says("Иван, добрый день! Высылаю материалы", at(6, 13))
    await e.tick(at(6, 14, 5))
    assert len(e.sent) == 1


async def test_viktor_writing_at_night_cancels_pending_welcome(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 23))
    await e.viktor_says("Добрый вечер! Завтра пришлю всё", at(6, 23, 5))
    await e.tick(at(7, 7, 0))
    assert e.sent == []


async def test_bot_was_down_too_long_goes_to_morning_tasks(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    # бот «лежав» добу: нагадування мало піти о 14:00, а 24 години минули о 11:55 наступного дня
    await e.tick(at(7, 12, 30))
    assert len(e.sent) == 1
    assert e.client.stage == "бот не встиг написати — задача Віктору"
    tasks = e.service.create_daily_tasks("2026-11-08", at(8, 7))
    assert [(t.chat_id, t.kind) for t in tasks] == [(CLIENT, "missed")]
    assert "бот не встиг нагадати" in tasks[0].note


async def test_bot_restarted_in_evening_reschedules_to_morning(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 18))
    await e.tick(at(6, 21, 0))       # нагадування мало бути о 20:00, але бот «прокинувся» о 21:00
    assert len(e.sent) == 1
    await e.tick(at(7, 7, 0))        # о 7:00 ще в межах 24 годин (до 17:55)
    assert e.texts()[1].startswith(REMINDER_START)


async def test_telegram_error_goes_to_tasks_and_admins(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    e.send_fails = True
    await e.tick(at(6, 14))
    assert any("Не вдалося надіслати нагадування" in m for m in e.admin)
    tasks = e.service.create_daily_tasks("2026-11-07", at(7, 7))
    assert [t.kind for t in tasks] == ["missed"]


async def test_silent_after_reminder_goes_to_tasks_once(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.tick(at(6, 14))
    day2 = e.service.create_daily_tasks("2026-11-07", at(7, 7))
    assert [(t.kind, t.note) for t in day2] == [("no_reply", "не відповів ні на привітання, ні на нагадування")]
    day3 = e.service.create_daily_tasks("2026-11-08", at(8, 7))
    assert day3 == []  # не повторюємо щодня (через 10 днів потрапить у «давно не спілкувалися»)


async def test_reply_after_reminder(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.tick(at(6, 14))
    await e.client_says("Извините, был занят", at(6, 16))
    assert "(після нагадування)" in e.group[0]
    assert e.service.create_daily_tasks("2026-11-07", at(7, 7)) == []


async def test_viktor_wrote_after_reminder_no_silent_task(tmp_path):
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.tick(at(6, 14))
    await e.viktor_says("Иван, как ваши дела?", at(6, 18))
    assert e.service.create_daily_tasks("2026-11-07", at(7, 7)) == []


async def test_morning_message_has_sections(tmp_path):
    from bot.tasks import format_tasks_message
    e = Env(tmp_path)
    await e.client_says("бонус", at(6, 12))
    await e.tick(at(6, 14))
    tasks = e.service.create_daily_tasks("2026-11-07", at(7, 7))
    text = format_tasks_message("2026-11-07", tasks, at(7, 7), 10)
    assert "Мовчать після привітання і нагадування</b> — 1" in text
    assert "1. Іван · @ivan — не відповів" in text


async def test_no_group_notifier_is_fine(tmp_path):
    e = Env(tmp_path)
    e.service.notify_group = None
    await e.client_says("бонус", at(6, 12))
    await e.client_says("привіт", at(6, 12, 5))
    assert e.client.welcome_replied_at is not None
