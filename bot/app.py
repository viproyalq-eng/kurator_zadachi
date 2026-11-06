"""Підключення до Telegram: обробники оновлень, команди, запуск."""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

from telegram import BusinessConnection, Message, Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (Application, BusinessConnectionHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .config import Config
from .db import Connection, Database
from .logic import client_display_name, message_text
from .notify import Notifier
from .service import BotService, IncomingMessage
from .sheets import SheetWriter

log = logging.getLogger(__name__)

SHEETS_ERROR_COOLDOWN = 30 * 60
ERROR_COOLDOWN = 5 * 60

PRIVATE_ONLY = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE


def connection_from_tg(bc: BusinessConnection) -> Connection:
    rights = getattr(bc, "rights", None)
    can_reply = bool(getattr(rights, "can_reply", False)) if rights else bool(getattr(bc, "can_reply", False))
    owner_name = client_display_name(bc.user.first_name, bc.user.last_name)
    return Connection(id=bc.id, owner_user_id=bc.user.id, owner_name=owner_name,
                      can_reply=can_reply, is_enabled=bool(bc.is_enabled))


def incoming_from_tg(message: Message) -> IncomingMessage:
    chat = message.chat
    return IncomingMessage(
        business_connection_id=message.business_connection_id,
        chat_id=chat.id,
        message_id=message.message_id,
        date=message.date,
        from_user_id=message.from_user.id if message.from_user else None,
        sent_by_bot=message.sender_business_bot is not None,
        client_name=client_display_name(chat.first_name, chat.last_name),
        client_username=chat.username,
        text=message_text(message),
    )


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.sheets = SheetWriter(cfg.google_sheet_id, cfg.google_credentials) if cfg.sheets_enabled else None
        self.application = (
            Application.builder()
            .token(cfg.bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        bot = self.application.bot

        async def send_admin(chat_id: int, text: str) -> None:
            await bot.send_message(chat_id=chat_id, text=text)

        self.notifier = Notifier(cfg.admin_chat_id, send_admin)
        self.service = BotService(cfg, self.db, self.sheets, self.notifier,
                                  send=self._send_as_curator, resolve_owner=self._resolve_owner)
        self._flush_task: asyncio.Task | None = None
        self._sheets_failing = False
        self._register()

    # --- допоміжне для сервісу ---
    async def _send_as_curator(self, connection_id: str, chat_id: int, text: str):
        sent = await self.application.bot.send_message(
            chat_id=chat_id, text=text, business_connection_id=connection_id)
        return sent.message_id, sent.date

    async def _resolve_owner(self, connection_id: str) -> int | None:
        try:
            bc = await self.application.bot.get_business_connection(connection_id)
        except Exception:  # noqa: BLE001
            log.exception("Не вдалося отримати бізнес-підключення %s", connection_id)
            return None
        self.db.save_connection(connection_from_tg(bc), datetime.now(timezone.utc).isoformat())
        return bc.user.id

    # --- обробники ---
    def _register(self) -> None:
        app = self.application
        app.add_handler(BusinessConnectionHandler(self.on_business_connection))
        app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, self.on_business_message))
        app.add_handler(CommandHandler("start", self.cmd_start, filters=PRIVATE_ONLY))
        app.add_handler(CommandHandler("id", self.cmd_id, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("chatid", self.cmd_chatid, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("status", self.cmd_status, filters=PRIVATE_ONLY))
        app.add_error_handler(self.on_error)

    async def on_business_connection(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bc = update.business_connection
        conn = connection_from_tg(bc)
        self.db.save_connection(conn, datetime.now(timezone.utc).isoformat())
        if conn.is_enabled:
            reply = "так ✅" if conn.can_reply else "НІ ❌ — увімкніть дозвіл відповідати, інакше привітання не надсилатимуться"
            await self.notifier.notify(
                f"🔗 Бот підключено до бізнес-акаунта: {conn.owner_name}.\nМоже відповідати від імені акаунта: {reply}")
        else:
            await self.notifier.notify(f"🔌 Бот відключено від бізнес-акаунта: {conn.owner_name}.")

    async def on_business_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.business_message
        if message is None or message.chat.type != "private":
            return
        await self.service.handle_message(incoming_from_tg(message))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Привіт! Я бот куратора.\n\n"
            "/id — ваш Telegram ID\n"
            "/chatid — ID цього чату\n"
            "/status — стан бота (лише для адміна)")

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.message.reply_html(f"Ваш Telegram ID: <code>{user.id}</code>")

    async def cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        await update.message.reply_html(f"ID цього чату: <code>{chat.id}</code>")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.cfg.admin_chat_id:
            await update.message.reply_text("Спочатку додайте змінну ADMIN_CHAT_ID у Railway (ваш ID із команди /id).")
            return
        if update.effective_chat.id != self.cfg.admin_chat_id:
            await update.message.reply_text("Ця команда лише для адміна.")
            return
        s = self.db.stats()
        if self.sheets is None:
            sheet_line = "вимкнена (немає GOOGLE_SHEET_ID або GOOGLE_CREDENTIALS_JSON)"
        elif self._sheets_failing:
            sheet_line = f"❌ помилка запису, у черзі {self.sheets.pending_count()} рядків"
        else:
            sheet_line = f"✅ працює, у черзі {self.sheets.pending_count()} рядків"
        welcome_line = "задано ✅" if self.cfg.welcome_text else "НЕ задано (WELCOME_TEXT)"
        await update.message.reply_text(
            "📊 Стан бота\n"
            f"Клієнтів: {s['clients']} (нових {s['new_clients']}, старих {s['old_clients']})\n"
            f"Повідомлень у базі: {s['messages']}\n"
            f"Надіслано привітань: {s['welcomes']}\n"
            f"Привітальний текст: {welcome_line}\n"
            f"Таблиця: {sheet_line}")

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            # Буває кілька секунд під час оновлення в Railway, коли стара й нова версія працюють одночасно.
            log.warning("Conflict: інша копія бота ще працює: %s", err)
            return
        if isinstance(err, (NetworkError, TimedOut)):
            log.warning("Тимчасова проблема з мережею: %s", err)
            return
        log.error("Помилка під час обробки оновлення", exc_info=err)
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))[-1500:]
        await self.notifier.notify(
            f"⚠️ Помилка в боті: {type(err).__name__}: {err}\n\n{tb}",
            key=f"err:{type(err).__name__}", cooldown=ERROR_COOLDOWN)

    # --- таблиця ---
    async def _flush_sheets_once(self) -> None:
        if not self.sheets or not self.sheets.pending_count():
            return
        try:
            await asyncio.to_thread(self.sheets.flush)
        except Exception as e:  # noqa: BLE001
            log.exception("Помилка запису в Google Таблицю")
            self._sheets_failing = True
            await self.notifier.notify(
                f"⚠️ Не вдається записати в Google Таблицю: {type(e).__name__}: {e}\n"
                f"Рядки збережені в черзі й будуть дописані, щойно запрацює. "
                f"Перевірте GOOGLE_SHEET_ID і що таблицю відкрито для email сервісного акаунта (Редактор).",
                key="sheets", cooldown=SHEETS_ERROR_COOLDOWN)
            return
        if self._sheets_failing:
            self._sheets_failing = False
            await self.notifier.notify("✅ Запис у Google Таблицю знову працює.")

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sheet_flush_seconds)
            await self._flush_sheets_once()

    async def _post_init(self, application: Application) -> None:
        me = await application.bot.get_me()
        lines = [f"✅ Бот @{me.username} запущено."]
        if self.sheets:
            try:
                title = await asyncio.to_thread(self.sheets.check_connection)
                lines.append(f"📗 Таблиця підключена: «{title}».")
            except Exception as e:  # noqa: BLE001
                log.exception("Не вдалося підключитися до таблиці")
                self._sheets_failing = True
                lines.append(f"❌ Таблиця не підключилася: {type(e).__name__}: {e}")
        for w in self.cfg.warnings:
            lines.append(f"• {w}")
        await self.notifier.notify("\n".join(lines))
        if self.sheets:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _post_shutdown(self, application: Application) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        if self.sheets and self.sheets.pending_count():
            try:
                await asyncio.to_thread(self.sheets.flush)
            except Exception:  # noqa: BLE001
                log.exception("Не вдалося дописати таблицю перед зупинкою")
        self.db.close()

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
