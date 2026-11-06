"""Головна логіка: що робити з кожним повідомленням у бізнес-чатах."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .config import Config
from .db import CLIENT_NEW, CLIENT_OLD, Client, Database
from .logic import SENDER_BOT, SENDER_CLIENT, fmt_dt, is_bonus
from .notify import Notifier

log = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    business_connection_id: str
    chat_id: int
    message_id: int
    date: datetime
    from_user_id: int | None
    sent_by_bot: bool          # повідомлення надіслав бот (наш чи інший) від імені Віктора
    client_name: str
    client_username: str | None
    text: str


# Надсилає повідомлення від імені Віктора і повертає (message_id, date).
SendFn = Callable[[str, int, str], Awaitable[tuple[int, datetime]]]
# Дізнається, хто власник бізнес-підключення (user_id), якщо його ще немає в базі.
OwnerFn = Callable[[str], Awaitable[int | None]]


class BotService:
    def __init__(self, cfg: Config, db: Database, sheets, notifier: Notifier,
                 send: SendFn, resolve_owner: OwnerFn):
        self.cfg = cfg
        self.db = db
        self.sheets = sheets  # SheetWriter або None, якщо таблиця не налаштована
        self.notifier = notifier
        self.send = send
        self.resolve_owner = resolve_owner

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def _owner_id(self, connection_id: str) -> int | None:
        conn = self.db.get_connection(connection_id)
        if conn:
            return conn.owner_user_id
        return await self.resolve_owner(connection_id)

    async def handle_message(self, msg: IncomingMessage) -> None:
        owner_id = await self._owner_id(msg.business_connection_id)
        if msg.sent_by_bot:
            sender = SENDER_BOT
        elif owner_id is not None and msg.from_user_id == owner_id:
            sender = self.cfg.curator_name
        else:
            sender = SENDER_CLIENT

        client = self.db.get_client(msg.chat_id)
        wants_welcome = (
            sender == SENDER_CLIENT
            and is_bonus(msg.text, self.cfg.bonus_keywords)
            and (client is None or client.welcome_sent_at is None)
        )

        is_dup = not self.db.add_message(
            msg.chat_id, msg.message_id, msg.business_connection_id, sender, msg.text, msg.date.isoformat())
        if is_dup:
            return

        if client is None:
            client = Client(
                chat_id=msg.chat_id, business_connection_id=msg.business_connection_id,
                name=msg.client_name, username=msg.client_username,
                client_type=CLIENT_NEW if wants_welcome else CLIENT_OLD,
                first_seen=msg.date.isoformat(), last_message_at=None, last_from=None,
                welcome_sent_at=None, stage=None,
            )
        elif wants_welcome:
            client.client_type = CLIENT_NEW
        client.business_connection_id = msg.business_connection_id
        client.name = msg.client_name or client.name
        client.username = msg.client_username or client.username
        client.last_message_at = msg.date.isoformat()
        client.last_from = sender
        self.db.upsert_client(client)
        self._queue_rows(client, sender, msg.text, msg.date)

        if wants_welcome:
            await self._send_welcome(client)

    async def _send_welcome(self, client: Client) -> None:
        who = self._client_label(client)
        if not self.cfg.welcome_text:
            await self.notifier.notify(
                f"🎁 {who} написав «бонус», але привітальний текст ще не задано "
                f"(змінна WELCOME_TEXT у Railway). Відповідь не надіслано — напишіть клієнту вручну.")
            return
        try:
            message_id, date = await self.send(client.business_connection_id, client.chat_id, self.cfg.welcome_text)
        except Exception as e:  # noqa: BLE001
            log.exception("Не вдалося надіслати привітання")
            await self.notifier.notify(
                f"⚠️ Не вдалося надіслати привітання клієнту {who}.\nПричина: {e}\n"
                f"Перевірте, що в налаштуваннях Telegram Business боту дозволено відповідати на повідомлення.")
            return
        client.welcome_sent_at = date.isoformat()
        client.last_message_at = date.isoformat()
        client.last_from = SENDER_BOT
        self.db.add_message(client.chat_id, message_id, client.business_connection_id,
                            SENDER_BOT, self.cfg.welcome_text, date.isoformat())
        self.db.upsert_client(client)
        self._queue_rows(client, SENDER_BOT, self.cfg.welcome_text, date)

    @staticmethod
    def _client_label(client: Client) -> str:
        return f"{client.name} (@{client.username})" if client.username else f"{client.name}"

    def _queue_rows(self, client: Client, sender: str, text: str, date: datetime) -> None:
        if not self.sheets:
            return
        tz = self.cfg.timezone
        self.sheets.queue_message([
            fmt_dt(date, tz), client.name or "", f"@{client.username}" if client.username else "",
            str(client.chat_id), sender, text,
        ])
        self.sheets.queue_client(client.chat_id, client_row(client, tz))


def _fmt_iso(value: str | None, tz) -> str:
    return fmt_dt(datetime.fromisoformat(value), tz) if value else ""


def client_row(client: Client, tz) -> list:
    return [
        str(client.chat_id), client.name or "", f"@{client.username}" if client.username else "",
        client.client_type, _fmt_iso(client.first_seen, tz), _fmt_iso(client.last_message_at, tz),
        client.last_from or "", _fmt_iso(client.welcome_sent_at, tz), client.stage or "",
    ]
