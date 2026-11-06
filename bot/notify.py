"""Сповіщення вам (адміну) у Telegram: про помилки, підключення, запуск."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

MAX_LEN = 3500


class Notifier:
    def __init__(self, admin_chat_id: int | None, send: Callable[[int, str], Awaitable[None]] | None,
                 clock: Callable[[], float] = time.monotonic):
        self.admin_chat_id = admin_chat_id
        self._send = send
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    async def notify(self, text: str, *, key: str | None = None, cooldown: float = 0) -> bool:
        """Надсилає повідомлення адміну. Якщо задано key — не частіше, ніж раз на cooldown секунд."""
        log.info("ADMIN: %s", text)
        if key and cooldown:
            now = self._clock()
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown:
                return False
            self._last_sent[key] = now
        if not self.admin_chat_id or not self._send:
            return False
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN] + "…"
        try:
            await self._send(self.admin_chat_id, text)
            return True
        except Exception:  # noqa: BLE001 — сповіщення не має ламати бота
            log.exception("Не вдалося надіслати сповіщення адміну")
            return False
