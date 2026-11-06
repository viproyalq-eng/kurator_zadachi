"""Сповіщення вам (адміну) у Telegram: про помилки, підключення, запуск."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

MAX_LEN = 3500


class Notifier:
    def __init__(self, admin_chat_ids: list[int] | int | None,
                 send: Callable[[int, str], Awaitable[None]] | None,
                 clock: Callable[[], float] = time.monotonic):
        if admin_chat_ids is None:
            admin_chat_ids = []
        elif isinstance(admin_chat_ids, int):
            admin_chat_ids = [admin_chat_ids]
        self.admin_chat_ids = list(admin_chat_ids)
        self._send = send
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    async def notify(self, text: str, *, key: str | None = None, cooldown: float = 0) -> bool:
        """Надсилає повідомлення всім адмінам. Якщо задано key — не частіше, ніж раз на cooldown секунд."""
        log.info("ADMIN: %s", text)
        if key and cooldown:
            now = self._clock()
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown:
                return False
            self._last_sent[key] = now
        if not self.admin_chat_ids or not self._send:
            return False
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN] + "…"
        delivered = False
        for chat_id in self.admin_chat_ids:
            try:
                await self._send(chat_id, text)
                delivered = True
            except Exception:  # noqa: BLE001 — сповіщення не має ламати бота
                # Найчастіше: адмін ще не натиснув «Старт» у боті.
                log.exception("Не вдалося надіслати сповіщення адміну %s", chat_id)
        return delivered
