"""Статистика: склад клієнтів і робота куратора за періоди та по днях."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import CLIENT_NEW, STATUS_PARTNER, Client, Task

BOT_STARTED_KEY = "bot_started_at"
DAILY_DAYS = 30

PERIODS = ["Сьогодні", "Вчора", "7 днів", "30 днів", "Весь час"]

METRICS = [
    ("issued", "Задач видано"),
    ("done", "Задач виконано"),
    ("done_pct", "% виконання"),
    ("replied", "Клієнт відповів на дотик"),
    ("curator_chats", "Чатів, де писав куратор"),
    ("curator_messages", "Повідомлень куратора"),
    ("new_clients", "Нових клієнтів (бонус)"),
    ("new_partners", "Переведено в партнери"),
]


@dataclass
class Composition:
    new_investors: int = 0
    old_investors: int = 0
    partners: int = 0
    excluded: int = 0

    @property
    def total(self) -> int:
        return self.new_investors + self.old_investors + self.partners


@dataclass
class Report:
    updated_at: datetime
    composition: Composition
    periods: dict[str, dict[str, float]] = field(default_factory=dict)  # період -> метрика -> значення
    daily: list[dict] = field(default_factory=list)                    # останні 30 днів, від старих до нових


def _local_date(iso: str | None, tz: ZoneInfo) -> date | None:
    return datetime.fromisoformat(iso).astimezone(tz).date() if iso else None


def _period_ranges(today: date) -> dict[str, tuple[date | None, date]]:
    return {
        "Сьогодні": (today, today),
        "Вчора": (today - timedelta(days=1), today - timedelta(days=1)),
        "7 днів": (today - timedelta(days=6), today),
        "30 днів": (today - timedelta(days=29), today),
        "Весь час": (None, today),
    }


def composition(clients: list[Client]) -> Composition:
    c = Composition()
    for cl in clients:
        if cl.excluded:
            c.excluded += 1
        elif cl.status == STATUS_PARTNER:
            c.partners += 1
        elif cl.client_type == CLIENT_NEW:
            c.new_investors += 1
        else:
            c.old_investors += 1
    return c


def build_report(clients: list[Client], tasks: list[Task], curator_msgs: list[tuple[int, str]],
                 now_utc: datetime, tz: ZoneInfo) -> Report:
    """clients — усі клієнти (з «не клієнтами»), tasks — усі задачі,
    curator_msgs — (chat_id, дата) повідомлень куратора клієнтам від запуску бота."""
    today = now_utc.astimezone(tz).date()
    active = [c for c in clients if not c.excluded]

    # Події по днях (місцевий час)
    issued_by_day: dict[date, int] = {}
    done_by_day: dict[date, int] = {}
    replied_by_day: dict[date, int] = {}
    for t in tasks:
        d = date.fromisoformat(t.task_date)
        issued_by_day[d] = issued_by_day.get(d, 0) + 1
        if t.done_at:
            done_by_day[d] = done_by_day.get(d, 0) + 1
        if t.replied_at:
            replied_by_day[d] = replied_by_day.get(d, 0) + 1

    msgs_by_day: dict[date, int] = {}
    chats_by_day: dict[date, set] = {}
    for chat_id, iso in curator_msgs:
        d = _local_date(iso, tz)
        msgs_by_day[d] = msgs_by_day.get(d, 0) + 1
        chats_by_day.setdefault(d, set()).add(chat_id)

    new_clients_by_day: dict[date, int] = {}
    partners_by_day: dict[date, int] = {}
    for c in active:
        d = _local_date(c.welcome_sent_at, tz)
        if d:
            new_clients_by_day[d] = new_clients_by_day.get(d, 0) + 1
        if c.status == STATUS_PARTNER:
            # партнер без дати (переведений до появи цієї функції) рахуємо «на сьогодні»
            d = _local_date(c.partner_since, tz) or today
            partners_by_day[d] = partners_by_day.get(d, 0) + 1

    def in_range(d: date, start: date | None, end: date) -> bool:
        return (start is None or d >= start) and d <= end

    def total(by_day: dict[date, int], start, end) -> int:
        return sum(v for d, v in by_day.items() if in_range(d, start, end))

    periods: dict[str, dict[str, float]] = {}
    for name, (start, end) in _period_ranges(today).items():
        issued = total(issued_by_day, start, end)
        done = total(done_by_day, start, end)
        chats = set().union(*[s for d, s in chats_by_day.items() if in_range(d, start, end)] or [set()])
        periods[name] = {
            "issued": issued,
            "done": done,
            "done_pct": round(100 * done / issued) if issued else 0,
            "replied": total(replied_by_day, start, end),
            "curator_chats": len(chats),
            "curator_messages": total(msgs_by_day, start, end),
            "new_clients": total(new_clients_by_day, start, end),
            "new_partners": total(partners_by_day, start, end),
        }

    daily = []
    for i in range(DAILY_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        daily.append({
            "date": d,
            "issued": issued_by_day.get(d, 0),
            "done": done_by_day.get(d, 0),
            "replied": replied_by_day.get(d, 0),
            "curator_chats": len(chats_by_day.get(d, ())),
            "new_clients": new_clients_by_day.get(d, 0),
            "new_partners": partners_by_day.get(d, 0),
            "partners_total": sum(v for pd, v in partners_by_day.items() if pd <= d),
        })

    return Report(updated_at=now_utc, composition=composition(clients), periods=periods, daily=daily)


# --- вкладка «Статистика»: фіксоване розташування (на нього посилаються діаграми) ---
STATS_TAB = "Статистика"
COMPOSITION_FIRST_ROW = 3        # 0-based: рядок «Нові інвестори» (A4)
COMPOSITION_ROWS = 3             # нові, старі, партнери — для кругової діаграми
DAILY_HEADER_ROW = 20            # 0-based: заголовок таблиці по днях (A21)
DAILY_HEADER = ["Дата", "Задач видано", "Виконано", "Клієнт відповів", "Чатів куратора",
                "Нових клієнтів", "Нових партнерів", "Партнерів всього"]
GRID_WIDTH = 8


def report_grid(report: Report, tz: ZoneInfo) -> list[list]:
    """Готова сітка значень для вкладки «Статистика» (завжди однакового розміру)."""
    comp = report.composition
    rows: list[list] = [
        ["Статистика куратора", "", "Оновлено:", report.updated_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")],
        [],
        ["Клієнти", "Кількість"],
        ["Нові інвестори (бонус)", comp.new_investors],
        ["Старі інвестори", comp.old_investors],
        ["Партнери", comp.partners],
        ["Всього клієнтів", comp.total],
        ["Не клієнти (виключені)", comp.excluded],
        [],
        ["Робота куратора", *PERIODS],
    ]
    for key, label in METRICS:
        values = [report.periods[p][key] for p in PERIODS]
        if key == "done_pct":
            values = [f"{int(v)}%" for v in values]
        rows.append([label, *values])
    while len(rows) < DAILY_HEADER_ROW:
        rows.append([])
    rows.append(DAILY_HEADER)
    for d in report.daily:
        rows.append([d["date"].strftime("%d.%m"), d["issued"], d["done"], d["replied"], d["curator_chats"],
                     d["new_clients"], d["new_partners"], d["partners_total"]])
    return [r + [""] * (GRID_WIDTH - len(r)) for r in rows]


def summary_text(report: Report) -> str:
    """Короткий звіт для Telegram (/stats)."""
    comp = report.composition
    lines = [
        "📊 <b>Статистика</b>",
        f"Клієнтів: {comp.total} — нових інвесторів {comp.new_investors}, старих {comp.old_investors}, "
        f"партнерів {comp.partners}",
        "",
        "<b>Робота куратора</b>",
    ]
    for p in PERIODS:
        m = report.periods[p]
        lines.append(
            f"• {p}: задачі {int(m['done'])}/{int(m['issued'])} ({int(m['done_pct'])}%), "
            f"відповіли {int(m['replied'])}, писав у {int(m['curator_chats'])} чатів, "
            f"нових партнерів {int(m['new_partners'])}")
    return "\n".join(lines)


# --- діаграми й оформлення (запити до Google Sheets API) ---
CHART_COLUMN = 9  # діаграми праворуч від таблиць, починаючи зі стовпця J


def _range(sheet_id: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                                         "startColumnIndex": c0, "endColumnIndex": c1}]}}


def _position(sheet_id: int, row: int) -> dict:
    return {"overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": row,
                                               "columnIndex": CHART_COLUMN},
                                "widthPixels": 560, "heightPixels": 320}}


def chart_requests(sheet_id: int) -> list[dict]:
    first_daily = DAILY_HEADER_ROW              # рядок заголовка (headerCount=1)
    last_daily = DAILY_HEADER_ROW + 1 + DAILY_DAYS

    def series(col: int) -> dict:
        return {"series": _range(sheet_id, first_daily, last_daily, col, col + 1), "targetAxis": "LEFT_AXIS"}

    def basic(chart_type: str, cols: list[int], title: str) -> dict:
        return {
            "chartType": chart_type,
            "legendPosition": "BOTTOM_LEGEND",
            "headerCount": 1,
            "axis": [{"position": "BOTTOM_AXIS", "title": "Дата"},
                     {"position": "LEFT_AXIS", "title": title}],
            "domains": [{"domain": _range(sheet_id, first_daily, last_daily, 0, 1)}],
            "series": [series(c) for c in cols],
        }

    return [
        {"addChart": {"chart": {
            "spec": {"title": "Склад клієнтів",
                     "pieChart": {"legendPosition": "RIGHT_LEGEND",
                                  "domain": _range(sheet_id, COMPOSITION_FIRST_ROW,
                                                   COMPOSITION_FIRST_ROW + COMPOSITION_ROWS, 0, 1),
                                  "series": _range(sheet_id, COMPOSITION_FIRST_ROW,
                                                   COMPOSITION_FIRST_ROW + COMPOSITION_ROWS, 1, 2)}},
            "position": _position(sheet_id, 0)}}},
        {"addChart": {"chart": {
            "spec": {"title": "Задачі куратора по днях (видано / виконано / клієнт відповів)",
                     "basicChart": basic("COLUMN", [1, 2, 3], "Задач")},
            "position": _position(sheet_id, 17)}}},
        {"addChart": {"chart": {
            "spec": {"title": "Партнерів всього",
                     "basicChart": basic("LINE", [7], "Партнерів")},
            "position": _position(sheet_id, 34)}}},
    ]


def format_requests(sheet_id: int) -> list[dict]:
    def bold(r0: int, r1: int, c1: int, size: int = 10) -> dict:
        return {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": 0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": size}}},
            "fields": "userEnteredFormat.textFormat"}}

    return [
        bold(0, 1, 1, 14),
        bold(2, 3, 2),
        bold(9, 10, 1 + len(PERIODS)),
        bold(DAILY_HEADER_ROW, DAILY_HEADER_ROW + 1, GRID_WIDTH),
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 240}, "fields": "pixelSize"}},
    ]
