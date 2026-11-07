"""Читання експорту Telegram Desktop (result.json або zip з ним).

Беремо лише особисті чати (один на один). Групи, канали, боти, «Збережене»,
видалені акаунти й службові чати Telegram пропускаємо.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SERVICE_CHAT_IDS = {777000, 42777}  # «Telegram», службові повідомлення

MEDIA_LABELS = {
    "voice_message": "[голосове]",
    "video_message": "[відеокружечок]",
    "sticker": "[стікер]",
    "animation": "[GIF]",
    "video_file": "[відео]",
    "audio_file": "[аудіо]",
}


class ImportError_(Exception):
    """Файл не схожий на експорт Telegram — пояснення для людини в тексті."""


@dataclass
class ExportMessage:
    message_id: int
    date: datetime          # UTC
    from_owner: bool        # написав власник акаунта (Віктор)
    text: str


@dataclass
class ExportChat:
    chat_id: int
    name: str
    messages: list[ExportMessage] = field(default_factory=list)


@dataclass
class Export:
    owner_id: int | None
    chats: list[ExportChat]
    skipped_groups: int = 0      # групи, канали
    skipped_bots: int = 0
    skipped_other: int = 0       # «Збережене», видалені акаунти, службові, порожні


def load_bytes(data: bytes, filename: str = "") -> dict:
    """bytes файлу (json або zip) → словник експорту."""
    raw = bytes(data)
    if raw[:2] == b"PK" or filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".json")]
                if not names:
                    raise ImportError_("В архіві немає файлу result.json.")
                names.sort(key=lambda n: (not n.lower().endswith("result.json"), len(n)))
                raw = zf.read(names[0])
        except zipfile.BadZipFile as e:
            raise ImportError_("Не вдалося відкрити zip-архів.") from e
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ImportError_(
            "Це не JSON. В експорті Telegram треба вибрати формат «JSON» (не «HTML»).") from e


def _text(value) -> str:
    """Текст повідомлення. Посилання, «сховані» під словом (text_link → href), дописуємо в кінець —
    інакше не видно, яке відео надіслано."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in value)
        hidden = [str(p["href"]) for p in value if isinstance(p, dict) and p.get("href")]
        extra = [h for h in hidden if h not in text]
        return f"{text} {' '.join(extra)}".strip() if extra else text
    return ""


def _message_text(m: dict) -> str:
    text = _text(m.get("text")).strip()
    label = ""
    if m.get("photo"):
        label = "[фото]"
    elif m.get("media_type") in MEDIA_LABELS:
        label = MEDIA_LABELS[m["media_type"]]
    elif m.get("file"):
        label = "[файл]"
    elif m.get("contact_information"):
        label = "[контакт]"
    elif m.get("location_information"):
        label = "[локація]"
    elif m.get("poll"):
        label = "[опитування]"
    if label:
        return f"{label} {text}".strip()
    return text or "[інше]"


def _date(m: dict, tz: ZoneInfo) -> datetime | None:
    unix = m.get("date_unixtime")
    if unix:
        try:
            return datetime.fromtimestamp(int(unix), timezone.utc)
        except (TypeError, ValueError):
            pass
    raw = m.get("date")
    if raw:
        try:
            # старі експорти: місцевий час комп'ютера без часового поясу
            return datetime.fromisoformat(raw).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _user_id(from_id) -> int | None:
    if isinstance(from_id, str) and from_id.startswith("user"):
        try:
            return int(from_id[4:])
        except ValueError:
            return None
    return None


def parse_export(data: dict, tz: ZoneInfo, fallback_owner_id: int | None = None) -> Export:
    """fallback_owner_id — ID Віктора з бізнес-підключення, якщо в експорті немає «Інформації про акаунт»."""
    if not isinstance(data, dict):
        raise ImportError_("Файл не схожий на експорт Telegram.")
    if "chats" in data and isinstance(data["chats"], dict):
        chat_list = data["chats"].get("list", [])
    elif "messages" in data and "id" in data:
        chat_list = [data]  # експорт одного чату
    else:
        raise ImportError_("У файлі немає чатів. В експорті треба увімкнути «Особисті чати».")

    personal = data.get("personal_information") or {}
    owner = personal.get("user_id") or fallback_owner_id

    export = Export(owner_id=personal.get("user_id"), chats=[])
    for chat in chat_list:
        ctype = chat.get("type")
        if ctype in ("bot_chat",):
            export.skipped_bots += 1
            continue
        if ctype != "personal_chat":
            if ctype in ("saved_messages",):
                export.skipped_other += 1
            else:
                export.skipped_groups += 1
            continue
        chat_id = chat.get("id")
        name = (chat.get("name") or "").strip()
        if not isinstance(chat_id, int) or chat_id in SERVICE_CHAT_IDS or not name:
            export.skipped_other += 1  # службовий чат або видалений акаунт
            continue
        out = ExportChat(chat_id=chat_id, name=name)
        for m in chat.get("messages", []):
            if m.get("type") != "message":
                continue  # дзвінки, службові події
            date = _date(m, tz)
            if date is None or not isinstance(m.get("id"), int):
                continue
            sender = _user_id(m.get("from_id"))
            if owner is not None:
                from_owner = sender == owner
            else:
                from_owner = sender is not None and sender != chat_id
            out.messages.append(ExportMessage(m["id"], date, from_owner, _message_text(m)))
        if out.messages:
            export.chats.append(out)
        else:
            export.skipped_other += 1
    return export
