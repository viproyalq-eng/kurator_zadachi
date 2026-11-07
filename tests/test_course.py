"""Курс інвестора: теми за посиланнями, етапи, «пропав після теми», вкладки."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.config import load_config
from bot.course import (COURSE_WIDTH, FUNNEL_HEADER, FUNNEL_HEADER_ROW, STAGES, TOPICS, build_course_efficiency,
                        course_chart_specs, detect_topics, stages_header, stages_rows)
from bot.db import CLIENT_OLD, Client, Connection, Database
from bot.notify import Notifier
from bot.service import BotService, IncomingMessage

KYIV = ZoneInfo("Europe/Kyiv")
VIKTOR, CLIENT = 777, 100
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)   # 11:00 за Києвом

TOPIC_TEXT = {
    "t1": "Тема 1: Интерфейс CortX https://youtu.be/wWj68CilMN0",
    "t2": "Тема 2: Видео -https://youtu.be/VfiPt_Ib9Sg",
    "t3": "Тема 3: Видео -https://youtu.be/DYSB1DXN2So",
    "t4": "Видео - https://youtu.be/Y22t58EKwok",
    "t5": "Следующий шаг - присоединиться к нашему закрытому клубу CORTX 🔥",
    "t6_1": "Видео - https://youtu.be/jKNQDcV0LOA",
    "t6_2": "Видео - https://youtu.be/nXDMPnhg8Eo",
    "t7": "Видеоинструкция - https://youtu.be/ZO8wslI9dx4",
    "t11": "Видео - https://www.youtube.com/watch?v=hXllZ9_aK0g",
}


@pytest.mark.parametrize("key,text", list(TOPIC_TEXT.items()))
def test_detect_each_topic(key, text):
    assert detect_topics(text) == [key]


def test_detect_nothing_and_variants():
    assert detect_topics("Добрый день! Как успехи?") == []
    assert detect_topics(None) == []
    assert detect_topics("https://youtube.com/shorts/DYSB1DXN2So?si=x") == ["t3"]
    assert detect_topics("https://youtu.be/AAAAAAAAAAA") == []          # чуже відео
    assert len(TOPICS) == 12 and [k for k, _ in STAGES][-1] == "partner"


class Env:
    def __init__(self, tmp_path):
        self.cfg = load_config({"BOT_TOKEN": "1:A", "TASKS_CHAT_ID": "-1001"})
        self.db = Database(str(tmp_path / "bot.db"))
        self.db.save_connection(Connection("conn", VIKTOR, "Віктор", True, True), "now")
        self.group, self.now, self.mid = [], T0, 0

        async def send(conn, chat, text):
            self.mid += 1
            return 10_000 + self.mid, self.now

        async def group(text):
            self.group.append(text)

        async def owner(c):
            return VIKTOR

        self.service = BotService(self.cfg, self.db, None, Notifier(None, None), send, owner, notify_group=group)
        self.service._now = lambda: self.now

    async def say(self, who, text, at):
        self.now = at
        self.mid += 1
        await self.service.handle_message(IncomingMessage(
            "conn", CLIENT, self.mid, at, VIKTOR if who == "v" else CLIENT, False, "Іван", "ivan", text))

    @property
    def client(self):
        return self.db.get_client(CLIENT)


def day(n, h=0):
    return T0 + timedelta(days=n, hours=h)


async def test_live_course_flow_and_lost(tmp_path):
    e = Env(tmp_path)
    await e.say("c", "бонус", day(0))
    await e.say("c", "Привет, я из Киева", day(0, 1))
    await e.say("v", TOPIC_TEXT["t1"], day(0, 2))
    assert e.client.stage == "Тема 1" and e.client.stage_at == day(0, 2).isoformat()
    await e.say("c", "посмотрел", day(1))
    await e.say("v", TOPIC_TEXT["t2"], day(1, 1))
    stored = e.db.stages_of(CLIENT)
    assert {"bonus", "t1", "t2"} <= set(stored)

    # клієнт мовчить після теми 2: 1 доба — ще ні, 2 доби — «пропав»
    assert await e.service.check_lost(day(2, 2)) == 0
    assert await e.service.check_lost(day(3, 2)) == 1
    assert "пропав після <b>Тема 2</b>" in e.group[-1] and "Треба втрутитися" in e.group[-1]
    assert await e.service.check_lost(day(4, 2)) == 0          # лише один раз за тему

    # наступного ранку — задача Віктору
    tasks = e.service.create_daily_tasks("2026-11-06", day(4))
    lost = [t for t in tasks if t.kind == "lost"]
    assert len(lost) == 1 and "Тема 2" in lost[0].note
    # повторно задача не з'являється
    assert not [t for t in e.service.create_daily_tasks("2026-11-07", day(5)) if t.kind == "lost"]


async def test_not_lost_when_ball_with_viktor(tmp_path):
    e = Env(tmp_path)
    await e.say("c", "бонус", day(0))
    await e.say("v", TOPIC_TEXT["t1"], day(0, 1))
    await e.say("c", "у меня вопрос", day(0, 3))          # клієнт відповів — тепер черга Віктора
    assert await e.service.check_lost(day(5)) == 0


async def test_history_rescan_from_imported_messages(tmp_path):
    e = Env(tmp_path)
    old = (T0 - timedelta(days=200)).isoformat()
    e.db.upsert_client(Client(200, None, "Олег", None, CLIENT_OLD, old, old, "клієнт", None, None))
    rows = [(200, 1, None, "Віктор", TOPIC_TEXT["t1"], (T0 - timedelta(days=100)).isoformat()),
            (200, 2, None, "клієнт", "ок", (T0 - timedelta(days=99)).isoformat()),
            (200, 3, None, "Віктор", TOPIC_TEXT["t3"], (T0 - timedelta(days=90)).isoformat()),
            (200, 4, None, "Віктор", "Добрый день, как вы?", (T0 - timedelta(days=80)).isoformat())]
    e.db.add_messages_bulk(rows)
    assert e.service.rescan_course_history() == 1
    assert set(e.db.stages_of(200)) == {"t1", "t3"}
    assert e.db.get_client(200).stage == "Тема 3"
    assert e.service.rescan_course_history() == 0           # повторно — без змін


async def test_stages_tab_marks(tmp_path):
    e = Env(tmp_path)
    await e.say("c", "бонус", day(0))
    await e.say("c", "Привет", day(0, 1))
    for i, key in enumerate(["t1", "t2", "t3", "t4", "t5", "t6_1", "t7"]):   # 6 ч.2 пропущена
        await e.say("v", TOPIC_TEXT[key], day(i + 1))
        await e.say("c", "ок", day(i + 1, 1))
    header = stages_header()
    clients = e.db.all_clients(include_excluded=True)
    rows = stages_rows(clients, e.db.all_stages(), {CLIENT: e.db.last_message_at_by(CLIENT, ("клієнт",))}, KYIV)
    row = dict(zip(header, rows[0]))
    assert row["Тем отримано"] == "7 з 12" and row["Поточний етап"] == "Тема 7"
    assert row["Бонус"].startswith("✅") and row["Тема 1"] == "✅ 03.11.26"
    assert row["Тема 6 ч.2"] == "➖ пропущено" and row["Тема 8"] == "❌" and row["Партнер"] == ""


def test_old_client_pre_course_cells_are_dashes():
    stored = {300: {"t1": "2026-06-01T10:00:00+00:00"}}
    c = Client(300, None, "Олег", None, CLIENT_OLD, "2025-01-01T00:00:00+00:00", "2026-06-01T10:00:00+00:00",
               "Віктор", None, None)
    row = dict(zip(stages_header(), stages_rows([c], stored, {}, KYIV)[0]))
    assert row["Бонус"] == "—" and row["Тема 1"].startswith("✅") and row["Тема 2"] == "❌"


async def test_course_efficiency_funnel(tmp_path):
    e = Env(tmp_path)
    await e.say("c", "бонус", day(0))
    await e.say("c", "Привет", day(0, 1))
    await e.say("v", TOPIC_TEXT["t1"], day(1))
    await e.say("v", TOPIC_TEXT["t2"], day(3))          # тема 1 зайняла 2 дні
    await e.service.check_lost(day(6))                 # пропав після теми 2
    clients = e.db.all_clients(include_excluded=True)
    rep = build_course_efficiency(clients, e.db.all_stages(),
                                  {CLIENT: e.db.last_message_at_by(CLIENT, ("клієнт",))}, day(6), KYIV)
    rows = rep.rows
    assert rows[FUNNEL_HEADER_ROW] == FUNNEL_HEADER and all(len(r) == COURSE_WIDTH for r in rows)
    funnel = {r[0]: r for r in rows[FUNNEL_HEADER_ROW + 1: FUNNEL_HEADER_ROW + 1 + len(STAGES)]}
    assert funnel["Написав «бонус»"][1] == 1 and funnel["Тема 2"][1] == 1 and funnel["Тема 3"][1] == 0
    assert funnel["Тема 1"][4] == 2.0                   # днів на етапі
    assert funnel["Тема 2"][7] == 1 and funnel["Тема 2"][8] == 1   # зараз тут і «пропав» тут
    assert any(r[0] == "Найбільше пропадають після" and r[1] == "Тема 2" for r in rows)
    specs = course_chart_specs(5)
    src = specs[0][1]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0]
    assert (src["startRowIndex"], src["endRowIndex"]) == (FUNNEL_HEADER_ROW, FUNNEL_HEADER_ROW + 1 + len(STAGES))


async def test_partner_stage_from_manual_status(tmp_path):
    e = Env(tmp_path)
    await e.say("c", "бонус", day(0))
    e.service.set_status(CLIENT, "партнер", day(2))
    row = dict(zip(stages_header(), stages_rows(e.db.all_clients(), e.db.all_stages(), {}, KYIV)[0]))
    assert row["Партнер"].startswith("✅")
    assert await e.service.check_lost(day(9)) == 0       # партнерів «пропав» не стосується


def test_hidden_link_in_live_message_is_seen():
    from types import SimpleNamespace
    from bot.logic import message_text
    msg = SimpleNamespace(text="Тема 3: Видео", caption=None,
                          entities=[SimpleNamespace(type="text_link", url="https://youtu.be/DYSB1DXN2So")])
    text = message_text(msg)
    assert text == "Тема 3: Видео https://youtu.be/DYSB1DXN2So" and detect_topics(text) == ["t3"]


def test_hidden_link_in_export_and_reimport_updates_stages(tmp_path):
    from bot.importer import parse_export
    data = {"personal_information": {"user_id": VIKTOR}, "chats": {"list": [
        {"name": "Олег", "type": "personal_chat", "id": 300, "messages": [
            {"id": 1, "type": "message", "date_unixtime": "1700000000", "from_id": "user300", "text": "бонус"},
            {"id": 2, "type": "message", "date_unixtime": "1700003600", "from_id": f"user{VIKTOR}",
             "text": ["Тема 1: ", {"type": "text_link", "text": "Видео", "href": "https://youtu.be/wWj68CilMN0"}]},
        ]}]}}
    export = parse_export(data, KYIV)
    assert export.chats[0].messages[1].text == "Тема 1: Видео https://youtu.be/wWj68CilMN0"
    e = Env(tmp_path)
    # перший імпорт старою версією зберіг текст без посилання
    e.db.add_messages_bulk([(300, 2, None, "Віктор", "Тема 1: Видео", "2023-11-14T23:13:20+00:00")])
    report = e.service.import_export(export, None, T0)
    assert report.course_clients == {300} and "t1" in e.db.stages_of(300)
    assert "youtu.be" in e.db.curator_texts("Віктор")[0][2]      # текст у базі виправлено


def test_course_check_lists_unknown_videos():
    from bot.course import course_check
    texts = [(1, "2025-01-10T10:00:00+00:00", "Тема 1 https://youtu.be/wWj68CilMN0"),
             (2, "2025-01-11T10:00:00+00:00", "Тема 1 (старое видео) https://youtu.be/OLDvideo001"),
             (3, "2025-02-11T10:00:00+00:00", "Тема 1 (старое видео) https://youtu.be/OLDvideo001"),
             (3, "2025-02-12T10:00:00+00:00", "Добрый день!")]
    report = course_check(texts, KYIV)
    assert "з посиланням на YouTube: 3" in report and "упізнано як тему курсу: 1 (у 1 клієнтів)" in report
    assert "<code>OLDvideo001</code> — 2 раз, 2 клієнтам, 11.01.25–11.02.25" in report
