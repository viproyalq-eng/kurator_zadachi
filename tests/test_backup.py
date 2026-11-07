import io
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.backup import BackupError, extract_backup, is_backup_name, make_backup, replace_database, rotate
from bot.config import load_config
from bot.db import CLIENT_OLD, Client, Database

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)   # 03:00 за Києвом


def filled_db(path):
    db = Database(str(path))
    for i in range(3):
        db.upsert_client(Client(i, None, f"К{i}", None, CLIENT_OLD, "2026-01-01T00:00:00+00:00",
                                "2026-01-01T00:00:00+00:00", "клієнт", None, None))
    db.add_message(1, 1, None, "клієнт", "привіт", "2026-01-01T00:00:00+00:00")
    return db


def test_make_extract_and_replace(tmp_path):
    db = filled_db(tmp_path / "data" / "bot.db")
    zip_path, stats = make_backup(db.conn, tmp_path / "data" / "backups", NOW, KYIV)
    assert zip_path.name == "kurator-backup-2026-11-08_0300.zip" and is_backup_name(zip_path.name)
    assert stats == {"clients": 3, "messages": 1, "tasks": 0}

    restored = tmp_path / "restore.db"
    assert extract_backup(zip_path.read_bytes(), restored) == stats

    # «усе полетіло»: нова порожня база, підміняємо файлом з бекапу
    db.close()
    fresh = tmp_path / "new" / "bot.db"
    Database(str(fresh)).close()
    replace_database(fresh, restored)
    again = Database(str(fresh))
    assert again.stats()["clients"] == 3 and again.get_client(1).name == "К1"


def test_rotate_keeps_last_seven(tmp_path):
    db = filled_db(tmp_path / "bot.db")
    for day in range(10):
        make_backup(db.conn, tmp_path / "b", NOW + timedelta(days=day), KYIV)
    removed = rotate(tmp_path / "b")
    left = sorted(p.name for p in (tmp_path / "b").glob("*.zip"))
    assert len(removed) == 3 and len(left) == 7 and left[0].startswith("kurator-backup-2026-11-11")


def test_bad_files_are_rejected(tmp_path):
    with pytest.raises(BackupError, match="zip"):
        extract_backup(b"not a zip", tmp_path / "x.db")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("result.json", "{}")
    with pytest.raises(BackupError, match="bot.db"):
        extract_backup(buf.getvalue(), tmp_path / "x.db")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bot.db", b"garbage" * 100)
    with pytest.raises(BackupError):
        extract_backup(buf.getvalue(), tmp_path / "x.db")
    assert not (tmp_path / "x.db").exists()


def test_config_recipients():
    cfg = load_config({"BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "111, 222"})
    assert cfg.backup_recipients == [111] and cfg.backup_time.hour == 3
    cfg = load_config({"BOT_TOKEN": "1:A", "ADMIN_CHAT_ID": "111, 222", "BACKUP_CHAT_ID": "333"})
    assert cfg.backup_recipients == [333]


# --- у боті ---

@pytest.fixture
def app(tmp_path, monkeypatch):
    from bot.app import BotApp
    cfg = load_config({"BOT_TOKEN": "123:ABC", "ADMIN_CHAT_ID": "1, 2", "TASKS_CHAT_ID": "-1001",
                       "DB_PATH": str(tmp_path / "data" / "bot.db")})
    a = BotApp(cfg)
    a.docs, a.texts = [], []

    async def send_document(self, chat_id, document, filename, caption, **kw):
        a.docs.append((chat_id, filename, document.read(), caption))

    async def send_message(self, chat_id, text, **kw):
        a.texts.append((chat_id, text))

    monkeypatch.setattr(type(a.application.bot), "send_document", send_document)
    monkeypatch.setattr(type(a.application.bot), "send_message", send_message)
    return a


async def test_daily_backup_goes_only_to_owner(app):
    app.db.upsert_client(Client(5, None, "Іван", None, CLIENT_OLD, "2026-01-01T00:00:00+00:00",
                                "2026-01-01T00:00:00+00:00", "клієнт", None, None))
    assert await app.send_backup()
    assert [d[0] for d in app.docs] == [1]                     # лише вам — не другому адміну і не в групу
    chat_id, filename, data, caption = app.docs[0]
    assert filename.startswith("kurator-backup-") and "Клієнтів: 1" in caption
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["bot.db"]
    assert list(app.backup_dir.glob("kurator-backup-*.zip"))   # і копія на диску


def doc_update(user_id, data, name):
    replies = []

    async def reply(text, **kw):
        replies.append((text, kw.get("reply_markup")))

    document = SimpleNamespace(file_name=name, file_size=len(data), file_id="F")
    upd = SimpleNamespace(message=SimpleNamespace(document=document, reply_text=reply),
                          effective_user=SimpleNamespace(id=user_id))

    async def download():
        return bytearray(data)

    async def get_file(file_id):
        return SimpleNamespace(download_as_bytearray=download)

    return upd, SimpleNamespace(bot=SimpleNamespace(get_file=get_file)), replies


def button(data, user_id):
    edits = []

    async def answer(*a, **k):
        pass

    async def edit(text, **k):
        edits.append(text)

    q = SimpleNamespace(data=data, answer=answer, edit_message_text=edit, from_user=SimpleNamespace(id=user_id))
    return SimpleNamespace(callback_query=q), edits


async def test_restore_from_backup_file(app, tmp_path):
    # бекап «старої» бази з 3 клієнтами
    old = filled_db(tmp_path / "old.db")
    zip_path, _ = make_backup(old.conn, tmp_path / "b", NOW, KYIV)
    exits = []
    app._exit = lambda code: exits.append(code)

    upd, ctx, replies = doc_update(1, zip_path.read_bytes(), zip_path.name)
    await app.on_document(upd, ctx)
    text, markup = replies[-1]
    assert "клієнтів 3" in text and "Зараз у базі: клієнтів 0" in text
    assert markup.inline_keyboard[0][0].callback_data == "restore:yes"

    upd, edits = button("restore:yes", 1)
    await app.on_restore_button(upd, None)
    assert "Базу відновлено" in edits[-1]
    import asyncio
    await asyncio.sleep(2.2)
    assert exits == [1]                                        # бот перезапускається
    assert Database(app.cfg.db_path).stats()["clients"] == 3
    assert len(list(app.backup_dir.glob("kurator-backup-*.zip"))) >= 1   # страхова копія поточної бази


async def test_restore_rejected_for_strangers_and_cancel(app, tmp_path):
    old = filled_db(tmp_path / "old.db")
    zip_path, _ = make_backup(old.conn, tmp_path / "b", NOW, KYIV)
    upd, ctx, replies = doc_update(999, zip_path.read_bytes(), zip_path.name)
    await app.on_document(upd, ctx)
    assert "лише адміни" in replies[-1][0]

    upd, ctx, replies = doc_update(1, zip_path.read_bytes(), zip_path.name)
    await app.on_document(upd, ctx)
    upd, edits = button("restore:no", 1)
    await app.on_restore_button(upd, None)
    assert edits[-1] == "Відновлення скасовано." and app.db.stats()["clients"] == 0
