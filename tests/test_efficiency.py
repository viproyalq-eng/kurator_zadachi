from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import CLIENT_NEW, CLIENT_OLD, Client, Task
from bot.efficiency import (WIDTH, LiveMessage, RoundState, build_efficiency, fmt_minutes, pct,
                            response_times)
from bot.sheets import dashboard_format_requests

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)   # 17:00 за Києвом
CUR, CL = "Віктор", "клієнт"


def h(hours_ago):
    return NOW - timedelta(hours=hours_ago)


def iso(hours_ago):
    return h(hours_ago).isoformat()


def test_fmt_and_pct():
    assert fmt_minutes(None) == "—" and fmt_minutes(0.2) == "<1 хв" and fmt_minutes(45) == "45 хв"
    assert fmt_minutes(125) == "2 год 5 хв" and fmt_minutes(60) == "1 год"
    assert fmt_minutes(27 * 60) == "1 д 3 год"
    assert pct(1, 3) == "33%" and pct(0, 0) == "—"


def test_response_times():
    msgs = [
        LiveMessage(1, CL, h(10), "питання"), LiveMessage(1, CL, h(9), "ще питання"),   # одне очікування
        LiveMessage(1, "бот", h(8), "авто"),                                            # бот не закриває
        LiveMessage(1, CUR, h(7), "відповідь"),
        LiveMessage(2, CUR, h(5), "привіт"), LiveMessage(2, CL, h(4), "привіт!"),          # чекає 4 год
        LiveMessage(3, CL, h(1), "щойно"),                                               # чекає 1 год
    ]
    pairs, waiting = response_times(msgs, CUR, CL)
    assert pairs == [(h(10), h(7))]
    assert waiting == {2: (h(4), "привіт!"), 3: (h(1), "щойно")}


def client(cid, ctype=CLIENT_OLD, **kw):
    base = dict(chat_id=cid, business_connection_id=None, name=f"К{cid}", username=None, client_type=ctype,
                first_seen=iso(24 * 400), last_message_at=iso(24 * 5), last_from=CL, welcome_sent_at=None,
                stage=None)
    base.update(kw)
    return Client(**base)


def make():
    clients = [
        client(1, CLIENT_NEW, welcome_sent_at=iso(30), welcome_replied_at=iso(29), username="ivan"),
        client(2, CLIENT_NEW, welcome_sent_at=iso(5), reminder_sent_at=iso(3), welcome_replied_at=iso(2),
               status="партнер", partner_since=iso(1)),
        client(3, CLIENT_NEW, welcome_sent_at=iso(4), cold_since=iso(1)),
        client(4, last_message_at=iso(24 * 400)),
        client(5, status="партнер", partner_since=iso(24 * 3)),
        client(6, excluded=1),
    ]
    tasks = [
        Task("2026-11-07", 4, 1, iso(24 * 400), iso(10), iso(2), done_text="+", replied_at=iso(1)),
        Task("2026-11-07", 1, 2, iso(30), iso(10), iso(3), done_text="Добрый день! Как успехи?"),
        Task("2026-11-06", 5, 1, iso(30), iso(34), iso(30), done_text="Добрый день! Как успехи?"),
        Task("2026-11-01", 3, 1, iso(30), iso(24 * 6), None),          # протермінована
    ]
    msgs = [
        LiveMessage(1, CL, h(29), "привет, я из Киева"), LiveMessage(1, CUR, h(28.5), "Отлично!"),
        LiveMessage(2, CL, h(2), "извините, был занят"), LiveMessage(2, CUR, h(1), "ничего"),
        LiveMessage(4, CL, h(26), "есть вопрос по роботу"),                              # чекає >доби
        LiveMessage(6, CL, h(30), "друг"),                                                # не клієнт — не рахуємо
    ]
    rounds = RoundState(1, NOW - timedelta(days=10), covered=30, total=230, per_day=20,
                        history=[])
    return build_efficiency(clients, tasks, msgs, alive_30d=3, rounds=rounds, now_utc=NOW, tz=KYIV,
                            curator=CUR, client_sender=CL)


def find(rows, label):
    return next(r for r in rows if r[0] == label)


def test_dashboard_values():
    e = make()
    rows = e.rows
    assert all(len(r) == WIDTH for r in rows)
    assert rows[0][0] == "📈 Ефективність куратора — Віктор"
    # швидкість: дві відповіді — 30 хв (вчора) і 1 год (сьогодні)
    assert find(rows, "Відповідей клієнтам")[1:6] == [1, 1, 2, 2, 2]
    assert find(rows, "Середній час відповіді")[1] == "1 год"
    assert find(rows, "Відповіли протягом 1 год")[5] == "100%"
    assert find(rows, "Чекають відповіді зараз (понад 2 год)")[1] == 1
    assert find(rows, "…з них понад добу")[1] == 1
    # воронка (весь час): 3 привітання, 2 відповіли, 1 після нагадування, 1 партнер, 1 холодний
    assert find(rows, "Отримали привітання")[5] == 3
    assert find(rows, "% відповіли")[5] == "67%"
    assert find(rows, "…з них лише після нагадування")[5] == 1
    assert find(rows, "Конверсія бонус → партнер")[5] == "33%"
    assert find(rows, "Пішли в холодні")[5] == 1
    # якість дотиків
    assert find(rows, "Виконано задач")[5] == 3
    assert find(rows, "Короткі дотики (до 20 символів)")[5] == 1
    assert find(rows, "Однакові тексти дотиків")[5] == 2
    assert find(rows, "Протерміновані задачі (старші 3 днів)")[1] == 1
    # база: темп 3/день → 200 залишилось → ~67 днів
    assert find(rows, "Залишилось у колі")[1] == 200
    assert find(rows, "Темп (людей на день)")[1] == 3.0
    assert find(rows, "Прогноз завершення кола")[1] == "13.01.2027"
    assert find(rows, "   понад рік")[1] == 1
    # партнери: 2 (один новий, один зі старої бази), обидва в поточному колі
    assert find(rows, "Партнерів усього")[1] == 2
    assert find(rows, "   Коло №1 (поточне)")[1] == 2
    # список «чекають відповіді»
    waiting = [r for r in rows if r[0] == "К4"]
    assert waiting and waiting[0][3] == "1 д 2 год" and waiting[0][6] == "есть вопрос по роботу"


def test_styles_and_format_requests():
    e = make()
    kinds = {s for _, s in e.styles}
    assert {"title", "section", "colheader"} <= kinds
    sections = [e.rows[i][0] for i, s in e.styles if s == "section"]
    assert len(sections) == 7 and sections[0].startswith("⏱") and sections[-1].startswith("📬")
    reqs = dashboard_format_requests(9, len(e.rows), WIDTH, e.styles)
    assert reqs[0]["repeatCell"]["fields"] == "userEnteredFormat"
    assert any("backgroundColor" in r.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {})
               for r in reqs)


def test_empty_base():
    rounds = RoundState(1, NOW, 0, 0, 20)
    e = build_efficiency([], [], [], 0, rounds, NOW, KYIV, CUR, CL)
    assert find(e.rows, "Прогноз завершення кола")[1] == "—"
    assert any(r[0] == "Ніхто не чекає 🎉" for r in e.rows)
