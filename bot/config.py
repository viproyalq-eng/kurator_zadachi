"""Налаштування бота. Усе читається зі змінних Railway (environment variables)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .texts import WELCOME_TEXT as DEFAULT_WELCOME_TEXT


class ConfigError(Exception):
    pass


def _clean_multiline(value: str) -> str:
    # У Railway зручно писати переноси рядків як \n — перетворюємо їх на справжні.
    return value.replace("\\n", "\n").strip()


@dataclass
class Config:
    bot_token: str
    db_path: str = "bot.db"
    admin_chat_ids: list[int] = field(default_factory=list)
    google_sheet_id: str | None = None
    google_credentials: dict | None = None
    welcome_text: str = ""
    bonus_keywords: list[str] = field(default_factory=lambda: ["бонус"])
    curator_name: str = "Віктор"
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Kyiv"))
    sheet_flush_seconds: int = 5
    warnings: list[str] = field(default_factory=list)

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_sheet_id and self.google_credentials)


def load_config(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("Не задано змінну BOT_TOKEN (токен від BotFather).")

    warnings: list[str] = []

    # Кілька адмінів — через кому: 123456789, 987654321
    admin_chat_ids: list[int] = []
    for part in (env.get("ADMIN_CHAT_ID") or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            warnings.append(f"ADMIN_CHAT_ID: «{part}» не схоже на ID (має бути число), пропускаю.")
            continue
        if value not in admin_chat_ids:
            admin_chat_ids.append(value)

    sheet_id = (env.get("GOOGLE_SHEET_ID") or "").strip() or None
    creds_raw = (env.get("GOOGLE_CREDENTIALS_JSON") or "").strip()
    creds = None
    if creds_raw:
        try:
            creds = json.loads(creds_raw)
            if "client_email" not in creds or "private_key" not in creds:
                warnings.append("GOOGLE_CREDENTIALS_JSON не схожий на ключ сервісного акаунта.")
                creds = None
        except json.JSONDecodeError:
            warnings.append("GOOGLE_CREDENTIALS_JSON не вдалося прочитати: вставте весь вміст JSON-файлу від { до }.")
    if not sheet_id:
        warnings.append("Не задано GOOGLE_SHEET_ID — таблиця вимкнена.")
    if not creds_raw:
        warnings.append("Не задано GOOGLE_CREDENTIALS_JSON — таблиця вимкнена.")

    # Текст привітання: зі змінної WELCOME_TEXT, якщо вона задана, інакше — з bot/texts.py.
    welcome = _clean_multiline(env.get("WELCOME_TEXT") or "") or DEFAULT_WELCOME_TEXT.strip()

    keywords_raw = env.get("BONUS_KEYWORDS") or "бонус"
    keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]

    tz_name = (env.get("TIMEZONE") or "Europe/Kyiv").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        warnings.append(f"Невідомий TIMEZONE {tz_name!r}, використовую Europe/Kyiv.")
        tz = ZoneInfo("Europe/Kyiv")

    return Config(
        bot_token=token,
        db_path=(env.get("DB_PATH") or "bot.db").strip(),
        admin_chat_ids=admin_chat_ids,
        google_sheet_id=sheet_id,
        google_credentials=creds,
        welcome_text=welcome,
        bonus_keywords=keywords or ["бонус"],
        curator_name=(env.get("CURATOR_NAME") or "Віктор").strip(),
        timezone=tz,
        warnings=warnings,
    )
