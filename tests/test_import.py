import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.config import load_config
from bot.db import CLIENT_NEW, Client, Database
from bot.importer import ImportError_, load_bytes, parse_export
from bot.notify import Notifier
from bot.service import BotService

KYIV = ZoneInfo("Europe/Kyiv")
VIKTOR = 777
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def ts(days_ago):
    return str(int((NOW - timedelta(days=days_ago)).timestamp()))


def msg(mid, days_ago, from_id, text="", **extra):
    return {"id": mid, "type": "message", "date": "2026-01-01T10:00:00", "date_unixtime": ts(days_ago),
            "from": "x", "from_id": f"user{from_id}", "text": text, "text_entities": [], **extra}


def sample(with_owner=True):
    data = {
        "about": "Here is the data you requested.",
        "chats": {"about": "...", "list": [
            {"name": "Іван Петренко", "type": "personal_chat", "id": 100, "messages": [
                msg(1, 400, 100, "Добрый день, хочу узнать про робота"),
                msg(2, 399, VIKTOR, ["Привет, ", {"type": "bold", "text": "Иван"}, "!"]),
                {"id": 3, "type": "service", "date_unixtime": ts(398), "action": "phone_call"},
                msg(4, 398, 100, "", media_type="voice_message", file="(File not included)"),
            ]},
            {"name": "Олена", "type": "personal_chat", "id": 200, "messages": [
                msg(10, 5, VIKTOR, "Как успехи?"),
                msg(11, 4, 200, "Все хорошо", photo="(File not included)"),
            ]},
            {"name": "Робоча група", "type": "private_supergroup", "id": 300, "messages": [msg(1, 1, 5, "hi")]},
            {"name": "BotFather", "type": "bot_chat", "id": 93372553, "messages": [msg(1, 1, 93372553, "hi")]},
            {"name": None, "type": "personal_chat", "id": 400, "messages": [msg(1, 1, 400, "hi")]},
            {"name": "Telegram", "type": "personal_chat", "id": 777000, "messages": [msg(1, 1, 777000, "code")]},
            {"type": "saved_messages", "id": VIKTOR, "messages": [msg(1, 1, VIKTOR, "note")]},
            {"name": "Порожній", "type": "personal_chat", "id": 500, "messages": []},
        ]},
    }
    if with_owner:
        data["personal_information"] = {"user_id": VIKTOR, "first_name": "Віктор"}
    return data


def test_parse_personal_chats_only():
    e = parse_export(sample(), KYIV)
    assert e.owner_id == VIKTOR
    assert [c.chat_id for c in e.chats] == [100, 200]
    assert (e.skipped_groups, e.skipped_bots, e.skipped_other) == (1, 1, 4)
    ivan = e.chats[0]
    assert [m.message_id for m in ivan.messages] == [1, 2, 4]           # дзвінок пропущено
    assert [m.from_owner for m in ivan.messages] == [False, True, False]
    assert ivan.messages[1].text == "Привет, Иван!"
    assert ivan.messages[2].text == "[голосове]"
    assert e.chats[1].messages[1].text == "[фото] Все хорошо"
    assert ivan.messages[0].date == NOW - timedelta(days=400)


def test_fallback_owner_when_no_account_info():
    e = parse_export(sample(with_owner=False), KYIV, fallback_owner_id=VIKTOR)
    assert e.owner_id is None
    assert [m.from_owner for m in e.chats[0].messages] == [False, True, False]


def test_without_any_owner_guesses_by_chat_id():
    e = parse_export(sample(with_owner=False), KYIV)
    assert [m.from_owner for m in e.chats[0].messages] == [False, True, False]


def test_single_chat_export_and_old_date_format():
    data = {"name": "Іван", "type": "personal_chat", "id": 100,
            "messages": [{"id": 1, "type": "message", "date": "2024-05-01T12:00:00", "from_id": "user100",
                          "text": "hi"}]}
    e = parse_export(data, KYIV, VIKTOR)
    assert e.chats[0].messages[0].date == datetime(2024, 5, 1, 9, 0, tzinfo=timezone.utc)


def test_load_json_zip_and_errors():
    raw = json.dumps(sample(), ensure_ascii=False).encode("utf-8")
    assert load_bytes(b"\xef\xbb\xbf" + raw)["chats"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Telegram Desktop/DataExport/result.json", raw)
    assert load_bytes(buf.getvalue(), "export.zip")["chats"]
    with pytest.raises(ImportError_, match="JSON"):
        load_bytes(b"<!DOCTYPE html><html>", "messages.html")
    with pytest.raises(ImportError_, match="Особисті чати"):
        parse_export({"about": "x"}, KYIV)
    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(ImportError_, match="result.json"):
        load_bytes(empty_zip.getvalue(), "a.zip")


class Sheets:
    def __init__(self):
        self.clients = {}

    def queue_message(self, row):
        raise AssertionError("історія імпорту не має йти у вкладку «Повідомлення»")

    def queue_client(self, chat_id, row, new_row=None):
        self.clients[chat_id] = row

    def queue_upsert(self, tab, row, new_row=None):
        pass


def make_service(tmp_path):
    cfg = load_config({"BOT_TOKEN": "1:A"})
    db = Database(str(tmp_path / "bot.db"))
    sheets = Sheets()

    async def nothing(*a):
        return None

    return db, sheets, BotService(cfg, db, sheets, Notifier(None, None), nothing, nothing)


def test_import_into_database(tmp_path):
    db, sheets, service = make_service(tmp_path)
    # Олена вже писала після запуску бота (живий запис, з username)
    live = (NOW - timedelta(hours=1)).isoformat()
    db.upsert_client(Client(200, "conn", "Олена К.", "olena", CLIENT_NEW, live, live, "клієнт", live, None))
    bot_started = NOW - timedelta(days=4, hours=12)   # бот запущено між повідомленнями Олени

    report = service.import_export(parse_export(sample(), KYIV), bot_started, NOW)
    assert (report.chats, report.new_clients, report.updated_clients) == (2, 1, 1)
    assert report.messages == 4      # 3 від Івана + 1 від Олени (до запуску бота)
    assert report.ready_for_tasks == 1   # Іван: 398 днів; Олена писала годину тому

    ivan = db.get_client(100)
    assert ivan.client_type == "старий" and ivan.name == "Іван Петренко"
    assert ivan.last_message_at == (NOW - timedelta(days=398)).isoformat() and ivan.last_from == "клієнт"
    olena = db.get_client(200)
    assert olena.username == "olena" and olena.client_type == CLIENT_NEW and olena.last_message_at == live
    assert olena.first_seen == (NOW - timedelta(days=5)).isoformat()
    assert set(sheets.clients) == {100, 200}

    # повторний імпорт — без дублів
    again = service.import_export(parse_export(sample(), KYIV), bot_started, NOW)
    assert again.messages == 0 and again.new_clients == 0

    # і клієнт з'являється в ранкових задачах
    tasks = service.create_daily_tasks("2026-11-07", NOW + timedelta(hours=19))
    assert [t.chat_id for t in tasks] == [100]


# --- надсилання файлу боту ---

@pytest.fixture
def app(tmp_path):
    from bot.app import BotApp
    cfg = load_config({"BOT_TOKEN": "123:ABC", "ADMIN_CHAT_ID": "1", "DB_PATH": str(tmp_path / "bot.db")})
    a = BotApp(cfg)
    return a


def doc_update(user_id, data: bytes, name="result.json", size=None):
    replies = []

    async def reply(text, **kwargs):
        replies.append(text)

    document = SimpleNamespace(file_name=name, file_size=len(data) if size is None else size, file_id="F1")
    upd = SimpleNamespace(message=SimpleNamespace(document=document, reply_text=reply),
                          effective_user=SimpleNamespace(id=user_id))

    async def download():
        return bytearray(data)

    async def get_file(file_id):
        return SimpleNamespace(download_as_bytearray=download)

    ctx = SimpleNamespace(bot=SimpleNamespace(get_file=get_file))
    return upd, ctx, replies


async def test_document_import_by_admin(app):
    raw = json.dumps(sample(), ensure_ascii=False).encode()
    upd, ctx, replies = doc_update(1, raw)
    await app.on_document(upd, ctx)
    assert "Імпорт завершено" in replies[-1]
    assert "Особистих чатів: 2" in replies[-1] and "Не клієнт" in replies[-1]
    assert app.db.get_client(100) is not None and app._stats_dirty


async def test_document_rejected_for_strangers_and_bad_files(app):
    raw = json.dumps(sample()).encode()
    upd, ctx, replies = doc_update(42, raw)
    await app.on_document(upd, ctx)
    assert "лише адміни або Віктор" in replies[-1] and app.db.get_client(100) is None

    upd, ctx, replies = doc_update(1, raw, size=25_000_000)
    await app.on_document(upd, ctx)
    assert "20 МБ" in replies[-1] and "zip" in replies[-1]

    upd, ctx, replies = doc_update(1, b"<html></html>", name="messages.html")
    await app.on_document(upd, ctx)
    assert "result.json" in replies[-1]

    upd, ctx, replies = doc_update(1, b"<html></html>", name="result.json")
    await app.on_document(upd, ctx)
    assert "JSON" in replies[-1]


async def test_viktor_can_import(app):
    from bot.db import Connection
    app.db.save_connection(Connection("conn", VIKTOR, "Віктор", True, True), "now")
    upd, ctx, replies = doc_update(VIKTOR, json.dumps(sample(with_owner=False)).encode())
    await app.on_document(upd, ctx)
    assert any("Імпорт завершено" in r for r in replies)
    assert not any("немає даних акаунта" in r for r in replies)   # власника взяли з підключення
