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


def client_link(task: Task) -> str:
    name = html.escape(task.name or "Без імені")
    # tg://user?id= відкриває чат з людиною у Віктора (він з нею вже листувався).
    link = f'<a href="tg://user?id={task.chat_id}">{name}</a>'
    if task.username:
        link += f" (@{html.escape(task.username)})"
    return link


def format_tasks_message(task_date: str, tasks: list[Task], now_utc: datetime, min_days: int,
                         yesterday: tuple[int, int] | None = None, header: str | None = None) -> str:
    """Текст для групи «Задачі куратора» (HTML)."""
    lines = [header or f"☀️ <b>Задачі на {fmt_date(task_date)}</b>"]
    if yesterday and yesterday[1]:
        lines.append(f"Вчора виконано: {yesterday[0]} з {yesterday[1]}")
    lines.append("")
    if not tasks:
        lines.append(f"Сьогодні немає клієнтів, з якими не спілкувалися {min_days}+ днів 🎉")
        return "\n".join(lines)
    done = sum(1 for t in tasks if t.done_at)
    lines.append(f"Написати клієнтам, з якими давно не було діалогу ({len(tasks)}), "
                 f"найстаріші — першими:")
    for i, t in enumerate(tasks, start=1):
        d = days_since(t.last_contact_at, now_utc)
        when = f"{d} дн. тому" if d is not None else "давно"
        mark = "✅ " if t.done_at else ""
        lines.append(f"{i}. {mark}{client_link(t)} — {when}")
    lines.append("")
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
    return [fmt_date(task.task_date), task.name or "", f"@{task.username}" if task.username else "",
            str(task.chat_id), last, "" if days is None else str(days), done]
