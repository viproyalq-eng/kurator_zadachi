"""Перевірка зв'язки з бібліотекою Telegram на справжніх об'єктах (без інтернету)."""
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.error import Conflict

from bot.app import BotApp, connection_from_tg, incoming_from_tg
from bot.config import load_config

OWNER = {"id": 777, "is_bot": False, "first_name": "Віктор"}
CLIENT = {"id": 100, "is_bot": False, "first_name": "Іван", "last_name": "Петренко", "username": "ivan"}


def business_update(text, from_user=CLIENT, update_id=1, message_id=1, **extra):
    msg = {
        "message_id": message_id, "date": 1793952000, "business_connection_id": "conn-1",
        "chat": {"id": 100, "type": "private", "first_name": "Іван", "last_name": "Петренко", "username": "ivan"},
        "from": from_user, "text": text, **extra,
    }
    return Update.de_json({"update_id": update_id, "business_message": msg}, None)


def connection_update(enabled=True, can_reply=True):
    bc = {"id": "conn-1", "user": OWNER, "user_chat_id": 777, "date": 1793952000, "is_enabled": enabled,
          "rights": {"can_reply": can_reply}}
    return Update.de_json({"update_id": 2, "business_connection": bc}, None)


def test_incoming_from_tg():
    m = incoming_from_tg(business_update("Бонус").business_message)
    assert m.chat_id == 100 and m.text == "Бонус" and m.client_name == "Іван Петренко"
    assert m.client_username == "ivan" and m.from_user_id == 100 and not m.sent_by_bot


def test_incoming_sent_by_business_bot():
    upd = business_update("hi", from_user=OWNER,
                          sender_business_bot={"id": 5, "is_bot": True, "first_name": "b", "username": "as_kurator_bot"})
    assert incoming_from_tg(upd.business_message).sent_by_bot


def test_connection_from_tg():
    c = connection_from_tg(connection_update().business_connection)
    assert c.owner_user_id == 777 and c.can_reply and c.is_enabled and c.owner_name == "Віктор"


@pytest.fixture
def app(tmp_path):
    cfg = load_config({"BOT_TOKEN": "123:ABC", "ADMIN_CHAT_ID": "1, 2",
                       "DB_PATH": str(tmp_path / "data" / "bot.db"), "WELCOME_TEXT": "Вітаю!"})
    a = BotApp(cfg)
    a.admin = []
    a.sent = []

    async def admin_send(chat_id, text):
        a.admin.append(text)

    async def send(conn_id, chat_id, text):
        a.sent.append((conn_id, chat_id, text))
        from datetime import datetime, timezone
        return 555, datetime.now(timezone.utc)

    a.notifier._send = admin_send
    a.service.send = send
    return a


async def test_connection_then_bonus_flow(app):
    await app.on_business_connection(connection_update(), SimpleNamespace())
    assert "підключено" in app.admin[-1]
    await app.on_business_message(business_update("бонус"), SimpleNamespace())
    assert app.sent == [("conn-1", 100, "Вітаю!")]
    await app.on_business_message(business_update("дякую", from_user=OWNER, update_id=3, message_id=2), SimpleNamespace())
    assert app.db.get_client(100).last_from == "Віктор"


async def test_connection_without_reply_right_warns(app):
    await app.on_business_connection(connection_update(can_reply=False), SimpleNamespace())
    assert "НІ" in app.admin[-1]


async def test_conflict_error_is_silent(app):
    await app.on_error(None, SimpleNamespace(error=Conflict("terminated by other getUpdates request")))
    assert app.admin == []


async def test_other_error_notifies(app):
    try:
        raise ValueError("щось зламалось")
    except ValueError as e:
        err = e
    await app.on_error(None, SimpleNamespace(error=err))
    assert "щось зламалось" in app.admin[-1]


async def test_commands(app):
    replies = []

    async def reply_html(text):
        replies.append(text)

    upd = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=-100500),
                          message=SimpleNamespace(reply_html=reply_html, reply_text=reply_html))
    await app.cmd_id(upd, None)
    await app.cmd_chatid(upd, None)
    await app.cmd_status(upd, None)  # не адмін
    upd.effective_chat.id = 2  # другий адмін теж має доступ
    await app.cmd_status(upd, None)
    assert "42" in replies[0] and "-100500" in replies[1]
    assert "лише для адміна" in replies[2]
    assert "Клієнтів" in replies[3]


async def test_handlers_registered(app):
    assert len(app.application.handlers[0]) == 6


async def test_sheet_failure_notifies_once_then_recovers(app):
    class Flaky:
        fail = True
        def pending_count(self): return 1
        def flush(self):
            if self.fail:
                raise RuntimeError("PERMISSION_DENIED")
            return 1
    app.sheets = Flaky()
    await app._flush_sheets_once()
    await app._flush_sheets_once()
    assert sum("Google Таблицю" in m for m in app.admin) == 2  # один раз кожному з 2 адмінів
    app.sheets.fail = False
    await app._flush_sheets_once()
    assert "знову працює" in app.admin[-1]
