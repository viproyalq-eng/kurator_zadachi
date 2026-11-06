"""Коли боту можна писати клієнтам.

- Лише у «робочий час» (за замовчуванням 7:00–20:30 за Києвом).
- Лише протягом 24 годин після останнього повідомлення клієнта (обмеження Telegram для бізнес-ботів).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TELEGRAM_REPLY_WINDOW = timedelta(hours=24)
# Невеликий запас, щоб не впертися в межу 24 годин секунда в секунду.
SAFETY_MARGIN = timedelta(minutes=5)


def next_send_time(when_utc: datetime, tz: ZoneInfo, start: time, end: time) -> datetime:
    """Найближчий момент ≥ when_utc, що потрапляє в робочий час [start, end)."""
    local = when_utc.astimezone(tz)
    if local.time() < start:
        local = datetime.combine(local.date(), start, tzinfo=tz)
    elif local.time() >= end:
        local = datetime.combine(local.date() + timedelta(days=1), start, tzinfo=tz)
    return local.astimezone(when_utc.tzinfo)


def in_send_window(when_utc: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    return start <= when_utc.astimezone(tz).time() < end


def reply_deadline(last_client_message_utc: datetime) -> datetime:
    """До коли бот ще може написати клієнту."""
    return last_client_message_utc + TELEGRAM_REPLY_WINDOW - SAFETY_MARGIN
