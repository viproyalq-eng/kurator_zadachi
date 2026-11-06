from datetime import datetime, timedelta, timezone


from bot.config import load_config
from bot.db import CLIENT_NEW, CLIENT_OLD, Connection, Database
from bot.notify import Notifier
from bot.service import BotService, IncomingMessage

OWNER = 777
CONN = "conn-1"
T0 = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)


class FakeSheets:
    def __init__(self):
        self.messages, self.clients = [], {}

    def queue_message(self, row):
        self.messages.append(row)

    def queue_client(self, chat_id, row):
        self.clients[chat_id] = row


class Env:
    def __init__(self, tmp_path, welcome="Вітаю! Ось ваш бонус.", send_fails=False, store_conn=True):
        env = {"BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "1", "WELCOME_TEXT": welcome}
        self.cfg = load_config(env)
        self.cfg.welcome_text = welcome  # порожній рядок = «текст не задано»
        self.db = Database(str(tmp_path / "bot.db"))
        if store_conn:
            self.db.save_connection(Connection(CONN, OWNER, "Віктор", True, True), "now")
        self.sheets = FakeSheets()
        self.admin_msgs = []
        self.sent = []
        self.owner_lookups = 0

        async def admin_send(chat_id, text):
            self.admin_msgs.append(text)

        async def send(conn_id, chat_id, text):
            if send_fails:
                raise RuntimeError("BUSINESS_PEER_INVALID")
            self.sent.append((conn_id, chat_id, text))
            return 9000 + len(self.sent), T0 + timedelta(seconds=1)

        async def resolve(conn_id):
            self.owner_lookups += 1
            return OWNER

        self.service = BotService(self.cfg, self.db, self.sheets, Notifier(1, admin_send), send, resolve)

    async def msg(self, text, *, chat_id=100, message_id=1, from_user=100, by_bot=False, username="ivan"):
        await self.service.handle_message(IncomingMessage(
            business_connection_id=CONN, chat_id=chat_id, message_id=message_id, date=T0,
            from_user_id=from_user, sent_by_bot=by_bot, client_name="Іван", client_username=username, text=text))


async def test_new_client_bonus_gets_welcome(tmp_path):
    e = Env(tmp_path)
    await e.msg("Бонус!")
    assert e.sent == [(CONN, 100, "Вітаю! Ось ваш бонус.")]
    client = e.db.get_client(100)
    assert client.client_type == CLIENT_NEW
    assert client.welcome_sent_at is not None
    assert client.last_from == "бот"
    assert e.db.stats()["messages"] == 2
    senders = [row[4] for row in e.sheets.messages]
    assert senders == ["клієнт", "бот"]
    assert e.sheets.clients[100][3] == CLIENT_NEW
    assert e.sheets.clients[100][7] != ""  # «Привітання надіслано»


async def test_welcome_only_once(tmp_path):
    e = Env(tmp_path)
    await e.msg("бонус", message_id=1)
    await e.msg("бонус", message_id=2)
    assert len(e.sent) == 1


async def test_old_client_without_bonus(tmp_path):
    e = Env(tmp_path)
    await e.msg("Добрий день, є питання", message_id=1)
    assert e.sent == []
    assert e.db.get_client(100).client_type == CLIENT_OLD
    await e.msg("а де мій бонус?", message_id=2)
    assert e.sent == []


async def test_old_client_who_writes_exact_bonus_becomes_new(tmp_path):
    e = Env(tmp_path)
    await e.msg("привіт", message_id=1)
    await e.msg("бонус", message_id=2)
    assert len(e.sent) == 1
    assert e.db.get_client(100).client_type == CLIENT_NEW


async def test_curator_outgoing_message(tmp_path):
    e = Env(tmp_path)
    await e.msg("бонус", from_user=OWNER)
    assert e.sent == []
    client = e.db.get_client(100)
    assert client.client_type == CLIENT_OLD
    assert client.last_from == "Віктор"
    assert e.sheets.messages[0][4] == "Віктор"


async def test_message_sent_by_bot_is_marked(tmp_path):
    e = Env(tmp_path)
    await e.msg("щось", from_user=OWNER, by_bot=True)
    assert e.sheets.messages[0][4] == "бот"


async def test_no_welcome_text_notifies_admin(tmp_path):
    e = Env(tmp_path, welcome="")
    await e.msg("бонус")
    assert e.sent == []
    assert any("WELCOME_TEXT" in m for m in e.admin_msgs)
    assert e.db.get_client(100).welcome_sent_at is None


async def test_send_failure_notifies_admin_and_allows_retry(tmp_path):
    e = Env(tmp_path, send_fails=True)
    await e.msg("бонус")
    assert any("Не вдалося надіслати привітання" in m for m in e.admin_msgs)
    assert e.db.get_client(100).welcome_sent_at is None


async def test_duplicate_update_ignored(tmp_path):
    e = Env(tmp_path)
    await e.msg("бонус", message_id=5)
    await e.msg("бонус", message_id=5)
    assert len(e.sent) == 1
    assert len(e.sheets.messages) == 2  # клієнт + бот, дубль не записано


async def test_owner_resolved_when_connection_unknown(tmp_path):
    e = Env(tmp_path, store_conn=False)
    await e.msg("привіт", from_user=OWNER)
    assert e.owner_lookups == 1
    assert e.db.get_client(100).last_from == "Віктор"


async def test_works_without_sheets(tmp_path):
    e = Env(tmp_path)
    e.service.sheets = None
    await e.msg("бонус")
    assert len(e.sent) == 1
