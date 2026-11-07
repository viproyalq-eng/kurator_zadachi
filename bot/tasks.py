"""Щоденні задачі куратору: кого сьогодні треба обійти."""
from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import Task

LAST_RUN_KEY = "tasks_last_run_date"


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def is_due(now_local: datetime, run_at: time, last_run_date: str | None) -> bool:
    """Чи пора надсилати сьогоднішні задачі: час настав і сьогодні ще не надсилали.
    Якщо бот перезапускався о 7:00 — надішле, щойно запуститься."""
    return now_local.time() >= run_at and last_run_date != now_local.date().isoformat()


def cutoff_utc(now_utc: datetime, min_days: int) -> datetime:
    return now_utc - timedelta(days=min_days)


def days_since(iso_value: str | None, now_utc: datetime) -> int | None:
    if not iso_value:
        return None
    return max(0, (now_utc - datetime.fromisoformat(iso_value)).days)


def fmt_date(d: str | date) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d.%m.%Y")


def chat_url(chat_id: int, username: str | None) -> str:
    """Посилання на чат з людиною: t.me за username, інакше — веб-версія Telegram за ID.
    Веб-посилання відкриває чат, бо Віктор уже листувався з цією людиною."""
    if username:
        return f"https://t.me/{username}"
    return f"https://web.telegram.org/a/#{chat_id}"


def person_link(chat_id: int, name: str | None, username: str | None) -> str:
    """Ім'я + @username (Telegram сам робить його клікабельним) або посилання «відкрити чат»."""
    name_html = html.escape(name or "Без імені")
    if username:
        return f"{name_html} · @{html.escape(username)}"
    return f'{name_html} · <a href="{chat_url(chat_id, None)}">відкрити чат</a>'


def fmt_contact(iso_value: str | None, now_utc: datetime, tz: ZoneInfo) -> str:
    """«18.01.2025 (658 дн. тому)» — точна дата останнього спілкування."""
    if not iso_value:
        return "давно"
    d = days_since(iso_value, now_utc)
    when = datetime.fromisoformat(iso_value).astimezone(tz).strftime("%d.%m.%Y")
    return f"{when} ({d} дн. тому)"


def client_link(task: Task) -> str:
    return person_link(task.chat_id, task.name, task.username)


TASK_SECTIONS = [
    ("missed", "🔔 <b>Бот не встиг написати — зробіть касання</b>"),
    ("no_reply", "📭 <b>Мовчать після привітання і нагадування</b>"),
    ("stale", "🕰 <b>Давно не було діалогу</b> (найстаріші — першими)"),
]
TASK_KIND_LABELS = {
    "missed": "Бот не встиг написати",
    "no_reply": "Мовчить після нагадування",
    "stale": "Давно не спілкувалися",
}


def format_tasks_message(task_date: str, tasks: list[Task], now_utc: datetime, min_days: int,
                         yesterday: tuple[int, int] | None = None, header: str | None = None,
                         tz: ZoneInfo = ZoneInfo("Europe/Kyiv")) -> str:
    """Текст для групи «Задачі куратора» (HTML), розбитий на розділи за типом задачі."""
    lines = [header or f"☀️ <b>Задачі на {fmt_date(task_date)}</b>"]
    if yesterday and yesterday[1]:
        lines.append(f"Вчора виконано: {yesterday[0]} з {yesterday[1]}")
    lines.append("")
    if not tasks:
        lines.append(f"Сьогодні немає клієнтів, з якими не спілкувалися {min_days}+ днів 🎉")
        return "\n".join(lines)
    number = 0
    for kind, title in TASK_SECTIONS:
        section = [t for t in tasks if (t.kind or "stale") == kind]
        if not section:
            continue
        lines.append(f"{title} — {len(section)}")
        for t in section:
            number += 1
            mark = "✅ " if t.done_at else ""
            if kind == "stale":
                detail = "останній контакт " + fmt_contact(t.last_contact_at, now_utc, tz)
            else:
                detail = t.note or ""
            lines.append(f"{number}. {mark}{client_link(t)}" + (f" — {detail}" if detail else ""))
        lines.append("")
    done = sum(1 for t in tasks if t.done_at)
    if done:
        lines.append(f"Виконано: {done} з {len(tasks)}")
    lines.append("Задача виконана, щойно Віктор напише клієнту будь-яке повідомлення.")
    return "\n".join(lines)


def task_row(task: Task, tz: ZoneInfo) -> list:
    """Рядок для вкладки «Задачі» в таблиці. «Днів без контакту» — на момент створення задачі."""
    from .logic import fmt_dt
    last = fmt_dt(datetime.fromisoformat(task.last_contact_at), tz) if task.last_contact_at else ""
    days = days_since(task.last_contact_at, datetime.fromisoformat(task.created_at))
    done = fmt_dt(datetime.fromisoformat(task.done_at), tz) if task.done_at else ""
    replied = fmt_dt(datetime.fromisoformat(task.replied_at), tz) if task.replied_at else ""
    text = (task.done_text or "")[:500]
    return [fmt_date(task.task_date), task.name or "", f"@{task.username}" if task.username else "",
            str(task.chat_id), last, "" if days is None else str(days), done, text, replied,
            TASK_KIND_LABELS.get(task.kind or "stale", task.kind), chat_url(task.chat_id, task.username)]
