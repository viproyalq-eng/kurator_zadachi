"""Прості правила без Telegram і без Google — їх легко перевіряти тестами."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

SENDER_CLIENT = "клієнт"
SENDER_BOT = "бот"

_NON_LETTERS = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    text = _NON_LETTERS.sub(" ", text.lower().replace("ё", "е"))
    text = text.replace("_", " ")
    return _SPACES.sub(" ", text).strip()


def is_bonus(text: str | None, keywords: list[str]) -> bool:
    """Тригер спрацьовує, коли клієнт надіслав саме кодове слово
    (з будь-якими великими/малими літерами, розділовими знаками чи емодзі).
    «Бонус», «бонус!», «БОНУС 🎁» — так. «А де мій бонус?» — ні."""
    norm = normalize(text)
    return bool(norm) and norm in {normalize(k) for k in keywords}


def fmt_dt(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def client_display_name(first_name: str | None, last_name: str | None) -> str:
    return " ".join(p for p in (first_name, last_name) if p).strip() or "Без імені"


MEDIA_LABELS = [
    ("photo", "[фото]"),
    ("video", "[відео]"),
    ("video_note", "[відеокружечок]"),
    ("voice", "[голосове]"),
    ("audio", "[аудіо]"),
    ("animation", "[GIF]"),
    ("document", "[файл]"),
    ("sticker", "[стікер]"),
    ("contact", "[контакт]"),
    ("location", "[локація]"),
    ("poll", "[опитування]"),
]


def message_text(message) -> str:
    """Текст повідомлення; для медіа — мітка типу плюс підпис, якщо є."""
    text = getattr(message, "text", None)
    if text:
        return text
    caption = getattr(message, "caption", None) or ""
    for attr, label in MEDIA_LABELS:
        if getattr(message, attr, None):
            return f"{label} {caption}".strip()
    return caption or "[інше]"
