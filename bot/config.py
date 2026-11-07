"""Налаштування бота. Усе читається зі змінних Railway (environment variables)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from .texts import REMINDER_TEXT as DEFAULT_REMINDER_TEXT
from .texts import TEAM_USERNAMES as DEFAULT_TEAM_USERNAMES
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
    tasks_chat_id: int | None = None
    tasks_time: time = field(default_factory=lambda: time(7, 0))
    tasks_per_day: int = 20
    tasks_min_days: int = 10
    team_usernames: set[str] = field(default_factory=set)
    reminder_text: str = ""
    reminder_delay_minutes: int = 120
    send_from: time = field(default_factory=lambda: time(7, 0))     # бот пише клієнтам лише з…
    send_until: time = field(default_factory=lambda: time(20, 30))  # …і до (за TIMEZONE)
    backup_chat_ids: list[int] = field(default_factory=list)  # кому надсилати бекап (порожньо — першому адміну)
    backup_time: time = field(default_factory=lambda: time(3, 0))

    @property
    def backup_recipients(self) -> list[int]:
        return self.backup_chat_ids or self.admin_chat_ids[:1]
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

    tasks_chat_id = None
    tasks_raw = (env.get("TASKS_CHAT_ID") or "").strip()
    if tasks_raw:
        try:
            tasks_chat_id = int(tasks_raw)
        except ValueError:
            warnings.append(f"TASKS_CHAT_ID має бути числом (з мінусом), зараз: {tasks_raw!r}")
    if tasks_chat_id is None:
        warnings.append("Не задано TASKS_CHAT_ID — щоденні задачі надсилатимуться адмінам, а не в групу.")

    tasks_time = time(7, 0)
    time_raw = (env.get("TASKS_TIME") or "").strip()
    if time_raw:
        try:
            h, m = time_raw.split(":")
            tasks_time = time(int(h), int(m))
        except ValueError:
            warnings.append(f"TASKS_TIME має бути у форматі 07:00, зараз: {time_raw!r}")

    def _int(name: str, default: int) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        warnings.append(f"{name} має бути додатним числом, зараз: {raw!r}. Використовую {default}.")
        return default

    def _hhmm(name: str, default: time) -> time:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            h, m = raw.split(":")
            return time(int(h), int(m))
        except ValueError:
            warnings.append(f"{name} має бути у форматі 07:00, зараз: {raw!r}")
            return default

    send_from = _hhmm("SEND_FROM", time(7, 0))
    send_until = _hhmm("SEND_UNTIL", time(20, 30))
    if send_from >= send_until:
        warnings.append("SEND_FROM має бути раніше за SEND_UNTIL — використовую 07:00–20:30.")
        send_from, send_until = time(7, 0), time(20, 30)

    backup_ids: list[int] = []
    for part in (env.get("BACKUP_CHAT_ID") or "").replace(";", ",").replace(" ", ",").split(","):
        if part.strip():
            try:
                backup_ids.append(int(part.strip()))
            except ValueError:
                warnings.append(f"BACKUP_CHAT_ID: «{part.strip()}» не схоже на ID, пропускаю.")

    team_raw = env.get("EXCLUDED_USERNAMES")
    team_list = team_raw.split(",") if team_raw is not None else DEFAULT_TEAM_USERNAMES
    team = {u.strip().lstrip("@").lower() for u in team_list if u.strip().lstrip("@")}

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
        tasks_chat_id=tasks_chat_id,
        tasks_time=tasks_time,
        tasks_per_day=_int("TASKS_PER_DAY", 20),
        tasks_min_days=_int("TASKS_MIN_DAYS", 10),
        team_usernames=team,
        reminder_text=_clean_multiline(env.get("REMINDER_TEXT") or "") or DEFAULT_REMINDER_TEXT.strip(),
        reminder_delay_minutes=_int("REMINDER_DELAY_MINUTES", 120),
        send_from=send_from,
        send_until=send_until,
        backup_chat_ids=backup_ids,
        backup_time=_hhmm("BACKUP_TIME", time(3, 0)),
        warnings=warnings,
    )
