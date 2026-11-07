"""Вкладка «Ефективність куратора»: усі показники роботи куратора й воронки в одному місці.

Модуль лише рахує і складає сітку для таблиці — без Telegram і Google.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import CLIENT_NEW, STATUS_PARTNER, Client, Task
from .stats import PERIODS, _local_date, _period_ranges
from .tasks import chat_url

EFFICIENCY_TAB = "Ефективність куратора"
WIDTH = 7  # A — показник, B–F — періоди, G — що означає

SHORT_TOUCH_CHARS = 20
OVERDUE_DAYS = 3
WAIT_ALERT_HOURS = 2
WEEKDAYS_FULL = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
HOUR_BUCKETS = [(7, 10), (10, 13), (13, 16), (16, 19), (19, 22)]  # решта — «ніч (22–7)»
RECENCY_BUCKETS = [(0, 7, "0–7 днів"), (8, 30, "8–30 днів"), (31, 90, "31–90 днів"),
                   (91, 365, "91–365 днів"), (366, 10 ** 6, "понад рік")]


@dataclass
class LiveMessage:
    chat_id: int
    sender: str
    date: datetime  # UTC
    text: str


@dataclass
class RoundState:
    number: int
    started_at: datetime
    covered: int
    total: int
    per_day: int
    history: list[dict] = field(default_factory=list)  # завершені кола: no, start, end, total


@dataclass
class Waiting:
    client: Client
    since: datetime
    last_text: str


# --- допоміжне ---

def fmt_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    m = int(round(minutes))
    if m < 1:
        return "<1 хв"
    if m < 60:
        return f"{m} хв"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} год {m} хв" if m else f"{h} год"
    d, h = divmod(h, 24)
    return f"{d} д {h} год" if h else f"{d} д"


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def _in(d: date | None, start: date | None, end: date) -> bool:
    return d is not None and (start is None or d >= start) and d <= end


# --- швидкість відповіді ---

def response_times(messages: list[LiveMessage], curator: str, client_sender: str
                   ) -> tuple[list[tuple[datetime, datetime]], dict[int, tuple[datetime, str]]]:
    """Пари (клієнт написав, Віктор відповів) і хто зараз чекає відповіді: chat_id -> (з якого часу, текст).
    Кілька повідомлень клієнта поспіль — це одне очікування, від першого з них.
    Повідомлення бота очікування не закривають."""
    pairs: list[tuple[datetime, datetime]] = []
    waiting: dict[int, tuple[datetime, str]] = {}
    by_chat: dict[int, list[LiveMessage]] = {}
    for m in messages:
        by_chat.setdefault(m.chat_id, []).append(m)
    for chat_id, msgs in by_chat.items():
        msgs.sort(key=lambda m: m.date)
        start: datetime | None = None
        last_text = ""
        for m in msgs:
            if m.sender == client_sender:
                if start is None:
                    start = m.date
                last_text = m.text
            elif m.sender == curator and start is not None:
                pairs.append((start, m.date))
                start = None
        if start is not None:
            waiting[chat_id] = (start, last_text)
    return pairs, waiting


# --- звіт ---

@dataclass
class Efficiency:
    updated_at: datetime
    rows: list[list] = field(default_factory=list)
    styles: list[tuple[int, str]] = field(default_factory=list)  # (номер рядка, стиль)


def build_efficiency(clients: list[Client], tasks: list[Task], messages: list[LiveMessage],
                     alive_30d: int, rounds: RoundState, now_utc: datetime, tz: ZoneInfo,
                     curator: str, client_sender: str) -> Efficiency:
    today = now_utc.astimezone(tz).date()
    ranges = _period_ranges(today)
    by_id = {c.chat_id: c for c in clients}
    active = [c for c in clients if not c.excluded]
    active_ids = {c.chat_id for c in active}
    live = [m for m in messages if m.chat_id in active_ids]

    rep = Efficiency(updated_at=now_utc)
    rows, styles = rep.rows, rep.styles

    def add(row: list, style: str = "") -> None:
        if style:
            styles.append((len(rows), style))
        rows.append(row + [""] * (WIDTH - len(row)))

    def section(title: str, periods: bool = True) -> None:
        add([])
        add([title], "section")
        if periods:
            add(["Показник", *PERIODS, "Що це означає"], "colheader")
        else:
            add(["Показник", "Значення", "", "", "", "", "Що це означає"], "colheader")

    def period_row(label: str, values: dict[str, object], note: str = "") -> None:
        add([label, *[values.get(p, "") for p in PERIODS], note])

    add([f"📈 Ефективність куратора — {curator}"], "title")
    add([f"Оновлено: {now_utc.astimezone(tz).strftime('%d.%m.%Y %H:%M')}. «Весь час» — від запуску бота."], "note")

    # 1. Швидкість відповіді
    pairs, waiting = response_times(live, curator, client_sender)
    section("⏱ 1. Швидкість відповіді клієнтам")
    stats: dict[str, dict[str, object]] = {k: {} for k in ("n", "avg", "med", "h1", "h24")}
    for p, (start, end) in ranges.items():
        mins = [(e - s).total_seconds() / 60 for s, e in pairs if _in(e.astimezone(tz).date(), start, end)]
        stats["n"][p] = len(mins)
        stats["avg"][p] = fmt_minutes(statistics.mean(mins)) if mins else "—"
        stats["med"][p] = fmt_minutes(statistics.median(mins)) if mins else "—"
        stats["h1"][p] = pct(sum(1 for x in mins if x <= 60), len(mins))
        stats["h24"][p] = pct(sum(1 for x in mins if x <= 24 * 60), len(mins))
    period_row("Відповідей клієнтам", stats["n"], "Скільки разів Віктор відповів людині, яка написала")
    period_row("Середній час відповіді", stats["avg"], "Від першого повідомлення клієнта до відповіді (з нічним часом)")
    period_row("Типовий час відповіді (медіана)", stats["med"], "Половина відповідей — швидше за це")
    period_row("Відповіли протягом 1 год", stats["h1"], "Чим ближче до 100%, тим краще")
    period_row("Відповіли протягом доби", stats["h24"], "Менше 90% — тривожний сигнал")
    long_wait = [(cid, s) for cid, (s, _) in waiting.items() if now_utc - s > timedelta(hours=WAIT_ALERT_HOURS)]
    day_wait = [x for x in long_wait if now_utc - x[1] > timedelta(hours=24)]
    add(["Чекають відповіді зараз (понад 2 год)", len(long_wait), "", "", "", "",
         "Список — у самому низу вкладки"])
    add(["…з них понад добу", len(day_wait), "", "", "", "", "Таких має бути 0"])

    # 2. Воронка нових
    section("🎁 2. Воронка нових клієнтів («бонус»)")
    cohorts = {p: [c for c in active if c.client_type == CLIENT_NEW
                   and _in(_local_date(c.welcome_sent_at, tz), s, e)] for p, (s, e) in ranges.items()}
    f: dict[str, dict[str, object]] = {k: {} for k in ("bonus", "rep", "rep_pct", "after", "partner",
                                                       "partner_pct", "days", "cold")}
    for p, cohort in cohorts.items():
        replied = [c for c in cohort if c.welcome_replied_at]
        after = [c for c in replied if c.reminder_sent_at and c.welcome_replied_at > c.reminder_sent_at]
        partners = [c for c in cohort if c.status == STATUS_PARTNER]
        days = [(datetime.fromisoformat(c.partner_since) - datetime.fromisoformat(c.welcome_sent_at)).days
                for c in partners if c.partner_since]
        f["bonus"][p] = len(cohort)
        f["rep"][p] = len(replied)
        f["rep_pct"][p] = pct(len(replied), len(cohort))
        f["after"][p] = len(after)
        f["partner"][p] = len(partners)
        f["partner_pct"][p] = pct(len(partners), len(cohort))
        f["days"][p] = round(statistics.mean(days), 1) if days else "—"
        f["cold"][p] = sum(1 for c in cohort if c.cold_since)
    period_row("Отримали привітання", f["bonus"], "Нові люди, які написали «бонус» (за датою привітання)")
    period_row("Відповіли на привітання", f["rep"])
    period_row("% відповіли", f["rep_pct"], "Наскільки працює привітання")
    period_row("…з них лише після нагадування", f["after"], "Скільки людей «повернуло» нагадування")
    period_row("Стали партнерами", f["partner"])
    period_row("Конверсія бонус → партнер", f["partner_pct"], "Головний показник результату")
    period_row("Днів від бонусу до партнера (середнє)", f["days"])
    period_row("Пішли в холодні", f["cold"])

    # 3. Якість дотиків
    section("✍️ 3. Якість роботи із задачами")
    q: dict[str, dict[str, object]] = {k: {} for k in ("done", "rep", "short", "dup")}
    for p, (start, end) in ranges.items():
        done = [t for t in tasks if t.done_at and _in(_local_date(t.done_at, tz), start, end)]
        texts = [(t.done_text or "").strip().lower() for t in done]
        counts: dict[str, int] = {}
        for txt in texts:
            if txt:
                counts[txt] = counts.get(txt, 0) + 1
        q["done"][p] = len(done)
        q["rep"][p] = pct(sum(1 for t in done if t.replied_at), len(done))
        q["short"][p] = sum(1 for txt in texts if len(txt) < SHORT_TOUCH_CHARS)
        q["dup"][p] = sum(1 for txt in texts if txt and counts[txt] > 1)
    period_row("Виконано задач", q["done"], "За датою, коли Віктор написав")
    period_row("Клієнт відповів на дотик", q["rep"], "Найчесніший показник якості дотиків")
    period_row(f"Короткі дотики (до {SHORT_TOUCH_CHARS} символів)", q["short"], "«+», «привет» — варто перевірити")
    period_row("Однакові тексти дотиків", q["dup"], "Скопійоване слово в слово — шаблонні дотики")
    overdue = {t.chat_id for t in tasks
               if not t.done_at and date.fromisoformat(t.task_date) <= today - timedelta(days=OVERDUE_DAYS)}
    add([f"Протерміновані задачі (старші {OVERDUE_DAYS} днів)", len(overdue), "", "", "", "",
         "Клієнти, яких видали, але так і не обійшли"])

    # 4. Стан бази
    section("🗂 4. Стан бази клієнтів", periods=False)
    remaining = max(0, rounds.total - rounds.covered)
    days_in_round = max(1, (now_utc - rounds.started_at).days)
    pace = rounds.covered / days_in_round
    use_pace = pace if days_in_round >= 3 and pace > 0 else rounds.per_day
    forecast = (today + timedelta(days=math.ceil(remaining / use_pace))).strftime("%d.%m.%Y") if remaining else "—"
    add([f"Коло №{rounds.number}: пройдено", f"{rounds.covered} з {rounds.total}", "", "", "", "",
         "Інвестори без партнерів, холодних і «не клієнтів»"])
    add(["Залишилось у колі", remaining])
    add(["Темп (людей на день)", round(pace, 1), "", "", "", "", f"План — {rounds.per_day} на день"])
    add(["Прогноз завершення кола", forecast, "", "", "", "", "За реальним темпом Віктора"])
    add(["«Живі» клієнти (самі писали за 30 днів)", alive_30d])
    add(["Давність останнього контакту:", "", "", "", "", "", "Чи «теплішає» база з часом"], "subheader")
    for lo, hi, label in RECENCY_BUCKETS:
        n = sum(1 for c in active if c.last_message_at
                and lo <= (now_utc - datetime.fromisoformat(c.last_message_at)).days <= hi)
        add([f"   {label}", n])

    # 5. Партнери
    section("🤝 5. Партнери", periods=False)
    partners = [c for c in active if c.status == STATUS_PARTNER]
    add(["Партнерів усього", len(partners)])
    add(["…прийшли через «бонус» (нові)", sum(1 for c in partners if c.client_type == CLIENT_NEW)])
    add(["…зі старої бази", sum(1 for c in partners if c.client_type != CLIENT_NEW),
         "", "", "", "", "Чи окупається обхід старої бази"])
    all_rounds = list(rounds.history) + [{"no": rounds.number, "start": rounds.started_at.isoformat(),
                                          "end": None, "total": rounds.total}]
    for r in all_rounds:
        start_iso, end_iso = r["start"], r.get("end")
        n = sum(1 for c in partners if c.partner_since and c.partner_since >= start_iso
                and (end_iso is None or c.partner_since < end_iso))
        label = f"   Коло №{r['no']}" + (" (поточне)" if end_iso is None else "")
        add([label, n, "", "", "", "", "Нових партнерів за час кола"])

    # 6. Коли працює куратор
    section("🕒 6. Коли працює куратор (останні 30 днів)", periods=False)
    month_ago = now_utc - timedelta(days=30)
    cur = [m for m in live if m.sender == curator and m.date >= month_ago]
    by_wd = [sum(1 for m in cur if m.date.astimezone(tz).weekday() == i) for i in range(7)]
    hours = [m.date.astimezone(tz).hour for m in cur]
    by_hour = [sum(1 for h in hours if a <= h < b) for a, b in HOUR_BUCKETS]
    night = sum(1 for h in hours if h >= 22 or h < 7)
    top = max(by_wd + by_hour + [night, 1])

    def bar(n: int) -> str:
        return "▇" * round(12 * n / top) if n else ""

    add(["Повідомлень куратора по днях тижня:", "", "", "", "", "", ""], "subheader")
    for name, n in zip(WEEKDAYS_FULL, by_wd):
        add([f"   {name}", n, "", "", "", "", bar(n)])
    add(["…по годинах:", "", "", "", "", "", ""], "subheader")
    for (a, b), n in zip(HOUR_BUCKETS, by_hour):
        add([f"   {a:02d}:00–{b:02d}:00", n, "", "", "", "", bar(n)])
    add(["   ніч 22:00–07:00", night, "", "", "", "", bar(night)])

    # 7. Список «чекають відповіді»
    add([])
    add([f"📬 7. Чекають відповіді Віктора (понад {WAIT_ALERT_HOURS} год) — найдовше першими"], "section")
    add(["Клієнт", "Username", "Чекає з", "Скільки чекає", "Посилання на чат", "", "Останнє повідомлення клієнта"],
        "colheader")
    if not long_wait:
        add(["Ніхто не чекає 🎉"])
    for chat_id, since in sorted(long_wait, key=lambda x: x[1]):
        c = by_id[chat_id]
        add([c.name or "Без імені", f"@{c.username}" if c.username else "",
             since.astimezone(tz).strftime("%d.%m %H:%M"),
             fmt_minutes((now_utc - since).total_seconds() / 60),
             chat_url(c.chat_id, c.username), "", (waiting[chat_id][1] or "")[:200]])
    return rep
