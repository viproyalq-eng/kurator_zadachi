from bot.db import CLIENT_OLD, Client, Connection, Database


def test_messages_dedup(tmp_path):
    db = Database(str(tmp_path / "sub" / "bot.db"))
    assert db.add_message(1, 10, "c", "клієнт", "hi", "2026-11-05T10:00:00+00:00")
    assert not db.add_message(1, 10, "c", "клієнт", "hi", "2026-11-05T10:00:00+00:00")
    assert db.stats()["messages"] == 1


def test_client_and_connection(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    c = Client(5, "conn", "Іван", "ivan", CLIENT_OLD, "2026-11-05T10:00:00+00:00", None, None, None, None)
    db.upsert_client(c)
    c.stage = "тема 1"
    db.upsert_client(c)
    assert db.get_client(5).stage == "тема 1"
    db.save_connection(Connection("conn", 777, "Віктор", True, True), "now")
    assert db.get_connection("conn").owner_user_id == 777
    assert db.get_connection("nope") is None
