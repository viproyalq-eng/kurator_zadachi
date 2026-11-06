from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import CLIENT_NEW, CLIENT_OLD, STATUS_PARTNER, Client, Task
from bot.stats import (COMPOSITION_FIRST_ROW, DAILY_HEADER, DAILY_HEADER_ROW, GRID_WIDTH, build_report,
                       chart_requests, report_grid, summary_text)

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)   # 14:00 за Києвом


def iso(days_ago=0, hours=0):
    return (NOW - timedelta(days=days_ago, hours=hours)).isoformat()


def client(chat_id, ctype=CLIENT_OLD, excluded=0, status="інвестор", partner_since=None, welcome=None):
    return Client(chat_id, "c", f"К{chat_id}", None, ctype, iso(100), iso(1), "клієнт", welcome, None,
                  excluded, status, partner_since)


def task(day_offset, chat_id, done=False, replied=False):
    d = (NOW.astimezone(KYIV).date() - timedelta(days=day_offset)).isoformat()
    return Task(d, chat_id, 1, iso(30), iso(day_offset), iso(day_offset) if done else None,
                replied_at=iso(day_offset) if replied else None)


def make_report():
    clients = [
        client(1, CLIENT_NEW, welcome=iso(0)),                       # новий сьогодні
        client(2, CLIENT_NEW, welcome=iso(3)),                       # новий 3 дні тому
        client(3),                                                    # старий
        client(4, status=STATUS_PARTNER, partner_since=iso(0)),      # партнер сьогодні
        client(5, CLIENT_NEW, status=STATUS_PARTNER, partner_since=iso(40)),
        client(6, excluded=1),                                       # не клієнт
    ]
    tasks = [task(0, 1, done=True, replied=True), task(0, 2, done=True), task(0, 3),
             task(1, 1, done=True), task(1, 2),
             task(10, 3, done=True)]
    msgs = [(1, iso(0)), (1, iso(0, 1)), (2, iso(0)), (3, iso(1)), (3, iso(20))]
    return build_report(clients, tasks, msgs, NOW, KYIV)


def test_composition():
    c = make_report().composition
    assert (c.new_investors, c.old_investors, c.partners, c.excluded, c.total) == (2, 1, 2, 1, 5)


def test_periods():
    p = make_report().periods
    assert p["Сьогодні"] == {"issued": 3, "done": 2, "done_pct": 67, "replied": 1, "curator_chats": 2,
                             "curator_messages": 3, "new_clients": 1, "new_partners": 1}
    assert p["Вчора"]["issued"] == 2 and p["Вчора"]["done"] == 1 and p["Вчора"]["curator_chats"] == 1
    assert p["7 днів"]["issued"] == 5 and p["7 днів"]["new_clients"] == 2
    assert p["30 днів"]["issued"] == 6 and p["30 днів"]["curator_messages"] == 5
    assert p["Весь час"]["new_partners"] == 2


def test_daily_table():
    daily = make_report().daily
    assert len(daily) == 30
    assert daily[-1]["date"] == NOW.astimezone(KYIV).date()
    assert daily[-1]["issued"] == 3 and daily[-1]["partners_total"] == 2
    assert daily[-2]["partners_total"] == 1      # учора був лише один партнер


def test_empty_report_has_no_division_by_zero():
    r = build_report([], [], [], NOW, KYIV)
    assert r.periods["Сьогодні"]["done_pct"] == 0
    assert "Клієнтів: 0" in summary_text(r)


def test_grid_layout_matches_charts():
    grid = report_grid(make_report(), KYIV)
    assert all(len(r) == GRID_WIDTH for r in grid)
    assert grid[COMPOSITION_FIRST_ROW][0].startswith("Нові інвестори")
    assert grid[COMPOSITION_FIRST_ROW + 2][0] == "Партнери"
    assert grid[DAILY_HEADER_ROW] == DAILY_HEADER
    assert len(grid) == DAILY_HEADER_ROW + 1 + 30
    assert grid[12][0] == "% виконання" and grid[12][1] == "67%"
    reqs = chart_requests(42)
    assert len(reqs) == 3
    pie = reqs[0]["addChart"]["chart"]["spec"]["pieChart"]["domain"]["sourceRange"]["sources"][0]
    assert (pie["startRowIndex"], pie["endRowIndex"]) == (COMPOSITION_FIRST_ROW, COMPOSITION_FIRST_ROW + 3)
    col = reqs[1]["addChart"]["chart"]["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0]
    assert (col["startRowIndex"], col["endRowIndex"]) == (DAILY_HEADER_ROW, DAILY_HEADER_ROW + 31)


def test_summary_text():
    text = summary_text(make_report())
    assert "партнерів 2" in text and "Сьогодні: задачі 2/3 (67%)" in text
