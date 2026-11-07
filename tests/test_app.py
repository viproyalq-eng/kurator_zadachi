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
    # «зараз» = 10:05 за Києвом того ж дня, що й тестові повідомлення (тести не залежать від годинника)
    from datetime import datetime as _dt, timezone as _tz
    a.service._now = lambda: _dt(2026, 11, 6, 8, 5, tzinfo=_tz.utc)
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
    assert len(app.application.handlers[0]) == 18


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


# --- група задач ---

@pytest.fixture
def sent_to(monkeypatch, app):
    """Перехоплює всі send_message бота: список (chat_id, text)."""
    class Calls(list):
        behaviour: dict

    calls = Calls()
    behaviour = {}

    async def fake_send(self, chat_id, text, **kwargs):
        calls.append((chat_id, text))
        err = behaviour.pop(chat_id, None)
        if err:
            raise err
        return None

    monkeypatch.setattr(type(app.application.bot), "send_message", fake_send)
    calls.behaviour = behaviour
    return calls


async def test_tasks_go_to_admins_when_group_not_set(app, sent_to):
    assert app.tasks_chat_id is None
    await app.send_daily_tasks()
    assert {c[0] for c in sent_to} == {1, 2}
    assert any("Задачі на" in c[1] for c in sent_to)
    assert any("TASKS_CHAT_ID" in m for m in app.admin)


async def test_tasks_go_to_group(app, sent_to):
    app.cfg.tasks_chat_id = -1001
    await app.send_daily_tasks()
    assert [c[0] for c in sent_to] == [-1001]


async def test_group_migration_is_remembered(app, sent_to):
    from telegram.error import ChatMigrated
    app.cfg.tasks_chat_id = -5
    sent_to.behaviour[-5] = ChatMigrated(-1009)
    await app.send_daily_tasks()
    assert [c[0] for c in sent_to] == [-5, -1009]
    assert app.tasks_chat_id == -1009
    assert any("-1009" in m for m in app.admin)


async def test_group_send_failure_falls_back_to_admins(app, sent_to):
    from telegram.error import Forbidden
    app.cfg.tasks_chat_id = -1001
    sent_to.behaviour[-1001] = Forbidden("bot is not a member")
    await app.send_daily_tasks()
    assert [c[0] for c in sent_to] == [-1001, 1, 2]
    assert any("Не вдалося надіслати задачі" in m for m in app.admin)


ADMIN_RIGHTS = {k: False for k in (
    "can_be_edited", "is_anonymous", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
    "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
    "can_post_stories", "can_edit_stories", "can_delete_stories")}


def member_update(status, chat_id=-1001, chat_type="supergroup"):
    bot_user = {"id": 5, "is_bot": True, "first_name": "Bot", "username": "as_kurator_bot"}
    data = {
        "update_id": 9,
        "my_chat_member": {
            "chat": {"id": chat_id, "type": chat_type, "title": "Задачі куратора"},
            "from": OWNER, "date": 1793952000,
            "old_chat_member": {"user": bot_user, "status": "left"},
            "new_chat_member": {"user": bot_user, "status": status, **(ADMIN_RIGHTS if status == "administrator" else {})},
        },
    }
    return Update.de_json(data, None)


async def test_bot_added_to_group_reports_id(app):
    await app.on_my_chat_member(member_update("member"), SimpleNamespace())
    assert "-1001" in app.admin[-1] and "TASKS_CHAT_ID" in app.admin[-1]
    app.cfg.tasks_chat_id = -1001
    await app.on_my_chat_member(member_update("administrator"), SimpleNamespace())
    assert "Задачі надходитимуть сюди" in app.admin[-1]
    await app.on_my_chat_member(member_update("left"), SimpleNamespace())
    assert "вилучили" in app.admin[-1]


async def test_cmd_tasks_permissions(app):
    replies = []

    async def reply(text, **kwargs):
        replies.append(text)

    def upd(chat_id, user_id):
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                               effective_user=SimpleNamespace(id=user_id),
                               message=SimpleNamespace(reply_text=reply, reply_html=reply))

    await app.cmd_tasks(upd(-777, 42), None)          # чужа група, не адмін
    assert "працює в групі задач" in replies[-1]
    app.cfg.tasks_chat_id = -1001
    await app.cmd_tasks(upd(-1001, 42), None)         # група задач — будь-хто з учасників
    assert "Задачі на" in replies[-1]
    await app.cmd_tasks(upd(1, 1), None)              # адмін в особистих
    assert "Задачі на" in replies[-1]


async def test_scheduler_runs_once_per_day(app, sent_to, monkeypatch):
    import bot.app as appmod
    app.cfg.tasks_chat_id = -1001
    from datetime import time
    app.cfg.tasks_time = time(0, 0)  # «вже пора»
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= 3:
            raise asyncio.CancelledError

    import asyncio
    monkeypatch.setattr(appmod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await app._scheduler_loop()
    assert [c[0] for c in sent_to] == [-1001]   # три «тіки», а надіслано лише раз


# --- /partner, кнопки, /stats ---

class Recorder:
    def __init__(self):
        self.replies, self.markups, self.answers, self.edits = [], [], [], []


def command_update(rec, chat_id, user_id, text=""):
    async def reply(text, reply_markup=None, **kwargs):
        rec.replies.append(text)
        rec.markups.append(reply_markup)

    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                           effective_user=SimpleNamespace(id=user_id),
                           message=SimpleNamespace(reply_text=reply, reply_html=reply, text=text))


def button_update(rec, data, chat_id, user_id):
    async def answer(text=None, show_alert=False):
        rec.answers.append(text)

    async def edit(text, **kwargs):
        rec.edits.append(text)

    query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit,
                            from_user=SimpleNamespace(full_name="Віктор"))
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id),
                           effective_user=SimpleNamespace(id=user_id))


async def test_partner_flow_in_tasks_group(app):
    app.cfg.tasks_chat_id = -1001
    await app.on_business_message(business_update("привіт"), SimpleNamespace())  # клієнт Іван Петренко, id 100
    rec = Recorder()
    await app.cmd_partner(command_update(rec, -1001, 555), SimpleNamespace(args=["іван"]))
    buttons = rec.markups[-1].inline_keyboard
    assert len(buttons) == 1 and buttons[0][0].callback_data == "partner:100"
    assert "Іван Петренко @ivan" in buttons[0][0].text

    await app.on_status_button(button_update(rec, "partner:100", -1001, 555), SimpleNamespace())
    assert "переведено в партнери" in rec.edits[-1] and "Віктор" in rec.edits[-1]
    assert app.db.get_client(100).status == "партнер"
    assert app._stats_dirty

    await app.on_status_button(button_update(rec, "partner:100", -1001, 555), SimpleNamespace())
    assert "вже партнер" in rec.edits[-1]

    # за @username — одразу, без кнопок
    await app.cmd_investor(command_update(rec, -1001, 555), SimpleNamespace(args=["@ivan"]))
    assert "Повернуто в інвестори: Іван Петренко (@ivan)" in rec.replies[-1]
    assert app.db.get_client(100).status == "інвестор"
    await app.on_status_button(button_update(rec, "investor:100", -1001, 555), SimpleNamespace())
    assert "вже інвестор" in rec.edits[-1]


async def test_partner_command_hints_and_permissions(app):
    app.cfg.tasks_chat_id = -1001
    rec = Recorder()
    await app.cmd_partner(command_update(rec, -1001, 555), SimpleNamespace(args=["нікого"]))
    assert "Не знайшов" in rec.replies[-1]
    await app.cmd_partner(command_update(rec, -999, 555), SimpleNamespace(args=["іван"]))  # чужа група
    assert "працює в групі задач" in rec.replies[-1]
    await app.on_status_button(button_update(rec, "partner:100", -999, 555), SimpleNamespace())
    assert app.db.get_client(100) is None or app.db.get_client(100).status == "інвестор"
    assert "лише в групі задач" in rec.answers[-1]


async def test_stats_command(app):
    rec = Recorder()
    await app.cmd_stats(command_update(rec, 1, 1), SimpleNamespace(args=[]))
    assert "Статистика" in rec.replies[-1] and "Робота куратора" in rec.replies[-1]


async def test_refresh_stats_writes_grid(app):
    written = []

    class Sheets:
        def write_stats(self, grid):
            written.append(grid)

        def write_partner_list(self, rows):
            pass

        def write_cold_list(self, rows):
            pass

    app.sheets = Sheets()
    await app.refresh_stats()
    assert written and written[0][0][0] == "Статистика куратора"
    assert not app._stats_dirty


async def test_partner_asks_for_name_then_shows_buttons(app):
    from telegram import ForceReply
    app.cfg.tasks_chat_id = -1001
    await app.on_business_message(business_update("привіт"), SimpleNamespace())  # клієнт id 100
    rec = Recorder()
    # натиснули /partner у меню — команда пішла без імені
    await app.cmd_partner(command_update(rec, -1001, 555), SimpleNamespace(args=[]))
    assert "Кого перевести в партнери" in rec.replies[-1] and "посилання на чат" in rec.replies[-1]
    assert isinstance(rec.markups[-1], ForceReply)

    # інший учасник групи пише щось своє — бот не реагує
    await app.on_name_reply(command_update(rec, -1001, 666, "привіт усім"), SimpleNamespace())
    assert len(rec.replies) == 1

    # ім'я з помилкою — бот перепитує і далі чекає
    await app.on_name_reply(command_update(rec, -1001, 555, "Мирослав"), SimpleNamespace())
    assert "Не знайшов" in rec.replies[-1]

    # ім'я, як підписаний — кнопки
    await app.on_name_reply(command_update(rec, -1001, 555, "Іван"), SimpleNamespace())
    assert rec.markups[-1].inline_keyboard[0][0].callback_data == "partner:100"

    # після цього звичайні повідомлення вже не перехоплюються
    count = len(rec.replies)
    await app.on_name_reply(command_update(rec, -1001, 555, "Іван"), SimpleNamespace())
    assert len(rec.replies) == count


async def test_partner_question_can_be_cancelled(app):
    app.cfg.tasks_chat_id = -1001
    rec = Recorder()
    await app.cmd_investor(command_update(rec, -1001, 555), SimpleNamespace(args=[]))
    assert "повернути в інвестори" in rec.replies[-1]
    await app.on_name_reply(command_update(rec, -1001, 555, "Скасувати"), SimpleNamespace())
    assert rec.replies[-1] == "Скасовано."
    assert not app._awaiting_name



async def test_cold_by_numbers_links_and_names(app):
    from datetime import datetime, timedelta, timezone
    from bot.db import CLIENT_OLD, Client
    app.cfg.tasks_chat_id = -1001
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    for cid, name, user in [(201, "Olya", None), (202, "Olya", None), (203, "Andriy B.", "andriy_b"),
                            (204, "Nik", None)]:
        app.db.upsert_client(Client(cid, None, name, user, CLIENT_OLD, old, old, "клієнт", None, None))
    tasks = app.service.create_daily_tasks(app._today_local().isoformat(), datetime.now(timezone.utc))
    assert len(tasks) == 4
    rec = Recorder()

    # номер зі списку + @username + посилання «відкрити чат» — одним повідомленням
    await app.cmd_cold(command_update(rec, -1001, 555),
                       SimpleNamespace(args=["1", "@andriy_b", "https://web.telegram.org/a/#204", "99"]))
    reply = rec.replies[-1]
    assert "Перенесено в холодні" in reply and "Andriy B. (@andriy_b)" in reply and "Nik" in reply
    assert "Не знайшов: №99" in reply
    first = tasks[0].chat_id
    assert app.db.get_client(first).cold_since and app.db.get_client(203).cold_since
    assert app.db.get_client(204).cold_since

    # однаково підписані — кнопки з різними id
    await app.cmd_cold(command_update(rec, -1001, 555), SimpleNamespace(args=["Olya"]))
    labels = [row[0].text for row in rec.markups[-1].inline_keyboard]
    assert len(labels) == 2 and labels[0] != labels[1] and all("id…" in label for label in labels)

    # холодні не потрапляють у задачі наступного дня
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1))
    next_day = app.service.create_daily_tasks((app._today_local() + timedelta(days=1)).isoformat(), tomorrow)
    assert {t.chat_id for t in next_day}.isdisjoint({first, 203, 204})

    # повернути з холодних
    await app.cmd_warm(command_update(rec, -1001, 555), SimpleNamespace(args=["@andriy_b"]))
    assert "Повернуто з холодних" in rec.replies[-1] and app.db.get_client(203).cold_since is None
    await app.cmd_warm(command_update(rec, -1001, 555), SimpleNamespace(args=["@andriy_b"]))
    assert "і так не в холодних" in rec.replies[-1].lower()


async def test_cold_list_written_to_sheet(app):
    from datetime import datetime, timezone
    from bot.db import CLIENT_OLD, Client
    app.db.upsert_client(Client(301, None, "Olya", None, CLIENT_OLD, "2025-01-18T10:00:00+00:00",
                                "2025-01-18T10:00:00+00:00", "клієнт", None, None))
    app.service.set_cold(301, True, datetime.now(timezone.utc))
    app.db.upsert_client(Client(302, None, "Andriy B.", "andriy_b", "новий", "2026-11-01T10:00:00+00:00",
                                "2026-11-06T10:00:00+00:00", "Віктор", None, None))
    app.service.set_status(302, "партнер", datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc))
    written = {}

    class Sheets:
        def write_stats(self, grid):
            written["stats"] = grid

        def write_cold_list(self, rows):
            written["cold"] = rows

        def write_partner_list(self, rows):
            written["partners"] = rows

        def queue_client(self, *a, **k):
            pass

    app.sheets = Sheets()
    await app.refresh_stats()
    row = written["cold"][0]
    assert row[0] == "Olya" and row[2] == "301" and row[7] == "https://web.telegram.org/a/#301"
    p = written["partners"]
    assert len(p) == 1 and p[0][0] == "Andriy B." and p[0][1] == "@andriy_b"
    assert p[0][3] == "07.11.2026 11:00" and p[0][4] == "новий" and p[0][9] == "https://t.me/andriy_b"
