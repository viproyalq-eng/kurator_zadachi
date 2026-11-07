"""Підключення до Telegram: обробники оновлень, команди, запуск."""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import date, datetime, timedelta, timezone

from telegram import (BotCommand, BusinessConnection, ChatMember, ForceReply, InlineKeyboardButton,
                      InlineKeyboardMarkup, Message, Update)
from telegram.constants import ParseMode
from telegram.error import ChatMigrated, Conflict, NetworkError, TimedOut
from telegram.ext import (Application, BusinessConnectionHandler, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from .config import Config
from .db import STATUS_INVESTOR, STATUS_PARTNER, Client, Connection, Database
from .logic import client_display_name, message_text
from .notify import Notifier
from .service import BotService, IncomingMessage
from .sheets import SheetWriter, explain_error
from .importer import ImportError_, load_bytes, parse_export
from .stats import BOT_STARTED_KEY, build_report, report_grid, summary_text
from .tasks import LAST_RUN_KEY, fmt_date, format_tasks_message, is_due

log = logging.getLogger(__name__)

SHEETS_ERROR_COOLDOWN = 30 * 60
ERROR_COOLDOWN = 5 * 60
SCHEDULER_TICK = 30           # як часто перевіряти, чи не пора надсилати задачі (сек)
FLAGS_SYNC_SECONDS = 10 * 60  # як часто читати позначки «Не клієнт» з таблиці
TASKS_CHAT_OVERRIDE_KEY = "tasks_chat_id_override"
STATS_SECONDS = 10 * 60       # як часто оновлювати вкладку «Статистика»
ASK_NAME_SECONDS = 10 * 60    # скільки бот чекає на ім'я клієнта після /partner без імені
MAX_DOWNLOAD_BYTES = 20 * 1000 * 1000  # Telegram дозволяє ботам завантажувати файли до 20 МБ
CANCEL_WORDS = {"скасувати", "отмена", "відміна", "cancel", "стоп"}

COMMANDS = [
    BotCommand("tasks", "Задачі на сьогодні"),
    BotCommand("partner", "Перевести клієнта в партнери"),
    BotCommand("investor", "Повернути клієнта в інвестори"),
    BotCommand("stats", "Статистика роботи куратора"),
    BotCommand("import", "Завантажити старі чати (файл експорту Telegram)"),
    BotCommand("chatid", "ID цього чату"),
    BotCommand("id", "Ваш Telegram ID"),
]

STATUS_ACTIONS = {
    "partner": (STATUS_PARTNER, "🤝", "переведено в партнери"),
    "investor": (STATUS_INVESTOR, "↩️", "повернуто в інвестори"),
}


def client_button_label(c: Client) -> str:
    label = c.name or "Без імені"
    if c.username:
        label += f" @{c.username}"
    if c.last_message_at:
        label += " · " + datetime.fromisoformat(c.last_message_at).strftime("%d.%m.%y")
    if c.status == STATUS_PARTNER:
        label = "🤝 " + label
    return label[:60]

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

        self.notifier = Notifier(cfg.admin_chat_ids, send_admin)
        self.service = BotService(cfg, self.db, self.sheets, self.notifier,
                                  send=self._send_as_curator, resolve_owner=self._resolve_owner,
                                  notify_group=self.send_to_tasks_chat)
        self._background: list[asyncio.Task] = []
        self._sheets_failing = False
        self._stats_dirty = True
        # (чат, користувач) -> (дія, коли спитали): бот чекає ім'я клієнта після /partner чи /investor
        self._awaiting_name: dict[tuple[int, int], tuple[str, float]] = {}
        self._register()

    def _allowed(self, update: Update) -> bool:
        """Робочі команди: у групі задач (будь-хто з учасників) або адміни в будь-якому чаті."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        return (chat_id is not None and chat_id == self.tasks_chat_id) or user_id in self.cfg.admin_chat_ids

    # --- група «Задачі куратора» ---
    @property
    def tasks_chat_id(self) -> int | None:
        override = self.db.get_setting(TASKS_CHAT_OVERRIDE_KEY)
        return int(override) if override else self.cfg.tasks_chat_id

    def _remember_migration(self, old_id: int, new_id: int) -> bool:
        if old_id == self.tasks_chat_id:
            self.db.set_setting(TASKS_CHAT_OVERRIDE_KEY, str(new_id))
            return True
        return False

    async def send_to_tasks_chat(self, text: str) -> bool:
        """Надсилає в групу задач. Якщо групу не задано — адмінам, щоб нічого не загубилося."""
        chat_id = self.tasks_chat_id
        if chat_id is None:
            await self.notifier.notify(
                "ℹ️ Групу задач ще не налаштовано (змінна TASKS_CHAT_ID), тому надсилаю вам:",
                key="no_tasks_chat", cooldown=12 * 3600)
            return await self._notify_html(text)
        bot = self.application.bot
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
            return True
        except ChatMigrated as e:
            # Група стала «супергрупою» і отримала новий ID — запам'ятовуємо й пробуємо ще раз.
            self._remember_migration(chat_id, e.new_chat_id)
            await self.notifier.notify(
                f"ℹ️ Група задач змінила ID: {chat_id} → {e.new_chat_id}. Бот уже користується новим, "
                f"але оновіть, будь ласка, змінну TASKS_CHAT_ID у Railway на {e.new_chat_id}.")
            await bot.send_message(chat_id=e.new_chat_id, text=text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("Не вдалося надіслати в групу задач")
            await self.notifier.notify(
                f"⚠️ Не вдалося надіслати задачі в групу ({chat_id}): {type(e).__name__}: {e}\n"
                f"Перевірте, що бот є учасником групи «Задачі куратора» і ID правильний (з мінусом). "
                f"Задачі нижче:", key="tasks_send", cooldown=600)
            await self._notify_html(text)
            return False

    async def _notify_html(self, text: str) -> bool:
        bot = self.application.bot
        ok = False
        for chat_id in self.cfg.admin_chat_ids:
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
                ok = True
            except Exception:  # noqa: BLE001
                log.exception("Не вдалося надіслати адміну %s", chat_id)
        return ok

    def _today_local(self) -> date:
        return datetime.now(self.cfg.timezone).date()

    async def _sync_not_client_flags(self) -> None:
        if not self.sheets:
            return
        try:
            flags = await asyncio.to_thread(self.sheets.read_not_client_flags)
        except Exception:  # noqa: BLE001 — не критично: використаємо те, що вже є в базі
            log.exception("Не вдалося прочитати позначки «Не клієнт»")
            return
        changed = self.service.apply_not_client_flags(flags)
        if changed:
            log.info("Оновлено позначки «Не клієнт»: %s", changed)

    async def send_daily_tasks(self, *, header: str | None = None) -> None:
        await self._sync_not_client_flags()
        now = datetime.now(timezone.utc)
        today = self._today_local()
        tasks = self.service.create_daily_tasks(today.isoformat(), now)
        y_tasks = self.db.tasks_for((today - timedelta(days=1)).isoformat())
        yesterday = (sum(1 for t in y_tasks if t.done_at), len(y_tasks)) if y_tasks else None
        text = format_tasks_message(today.isoformat(), tasks, now, self.cfg.tasks_min_days,
                                    yesterday=yesterday, header=header, tz=self.cfg.timezone)
        await self.send_to_tasks_chat(text)

    async def _scheduler_loop(self) -> None:
        last_flags_sync = 0.0
        last_stats = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Привітання й нагадування, яким настав час (лише в робочі години).
                await self.service.process_scheduled()
                now_local = datetime.now(self.cfg.timezone)
                if is_due(now_local, self.cfg.tasks_time, self.db.get_setting(LAST_RUN_KEY)):
                    # Позначаємо одразу, щоб у разі помилки не надсилати список кожні 30 секунд.
                    self.db.set_setting(LAST_RUN_KEY, now_local.date().isoformat())
                    await self.send_daily_tasks()
                    self._stats_dirty = True
                elif self.sheets and loop.time() - last_flags_sync > FLAGS_SYNC_SECONDS:
                    last_flags_sync = loop.time()
                    await self._sync_not_client_flags()
                if self.sheets and (self._stats_dirty or loop.time() - last_stats > STATS_SECONDS):
                    last_stats = loop.time()
                    # спершу дописуємо чергу, щоб статистика й інші вкладки збігалися
                    await self._flush_sheets_once()
                    await self.refresh_stats()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("Помилка в розкладі задач")
                await self.notifier.notify(f"⚠️ Помилка під час формування задач: {type(e).__name__}: {e}",
                                           key="scheduler", cooldown=ERROR_COOLDOWN)
            await asyncio.sleep(SCHEDULER_TICK)

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
        app.add_handler(CommandHandler("tasks", self.cmd_tasks, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("partner", self.cmd_partner, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("investor", self.cmd_investor, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("stats", self.cmd_stats, filters=filters.UpdateType.MESSAGE))
        app.add_handler(CommandHandler("import", self.cmd_import, filters=PRIVATE_ONLY))
        app.add_handler(MessageHandler(PRIVATE_ONLY & filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
                                       self.on_name_reply))
        app.add_handler(CallbackQueryHandler(self.on_status_button, pattern=r"^(partner|investor):-?\d+$"))
        app.add_handler(ChatMemberHandler(self.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, self.on_migrate))
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

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Бота додали в групу або вилучили з неї — повідомляємо адмінів разом з ID групи."""
        change = update.my_chat_member
        chat = change.chat
        if chat.type not in ("group", "supergroup"):
            return
        status = change.new_chat_member.status
        title = chat.title or "без назви"
        if status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
            if chat.id == self.tasks_chat_id:
                await self.notifier.notify(f"✅ Бот у групі задач «{title}». Задачі надходитимуть сюди.")
            else:
                await self.notifier.notify(
                    f"➕ Мене додали в групу «{title}».\nID групи: {chat.id}\n\n"
                    f"Якщо це група для задач Віктора — впишіть це число (разом з мінусом) у змінну "
                    f"TASKS_CHAT_ID у Railway і натисніть Deploy.")
        elif status in (ChatMember.LEFT, ChatMember.BANNED):
            await self.notifier.notify(f"➖ Мене вилучили з групи «{title}» (ID {chat.id}).")

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message and message.migrate_to_chat_id:
            old_id, new_id = message.chat.id, message.migrate_to_chat_id
            if self._remember_migration(old_id, new_id):
                await self.notifier.notify(
                    f"ℹ️ Група задач змінила ID: {old_id} → {new_id}. Бот уже користується новим, "
                    f"але оновіть змінну TASKS_CHAT_ID у Railway на {new_id}.")

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показати сьогоднішні задачі з позначками, що вже виконано."""
        if not self._allowed(update):
            await update.message.reply_text("Ця команда працює в групі задач або для адмінів.")
            return
        await self._sync_not_client_flags()
        now = datetime.now(timezone.utc)
        today = self._today_local()
        tasks = self.service.create_daily_tasks(today.isoformat(), now)
        text = format_tasks_message(today.isoformat(), tasks, now, self.cfg.tasks_min_days, tz=self.cfg.timezone)
        await update.message.reply_html(text, disable_web_page_preview=True)

    # --- партнери ---
    async def cmd_partner(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._status_command(update, context, "partner")

    async def cmd_investor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._status_command(update, context, "investor")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
        if not self._allowed(update):
            await update.message.reply_text("Ця команда працює в групі задач або для адмінів.")
            return
        query = " ".join(getattr(context, "args", None) or [])
        if not query:
            # Команду натиснули з меню без імені — питаємо ім'я і чекаємо відповідь.
            key = (update.effective_chat.id, update.effective_user.id)
            self._awaiting_name[key] = (action, asyncio.get_running_loop().time())
            who = "перевести в партнери" if action == "partner" else "повернути в інвестори"
            await update.message.reply_text(
                f"Кого {who}? Напишіть ім'я або @username клієнта (як він підписаний у Telegram).",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Ім'я або @username"),
                do_quote=True)
            return
        await self._offer_clients(update, action, query)

    async def on_name_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Відповідь з ім'ям клієнта після /partner або /investor без імені."""
        if not update.effective_chat or not update.effective_user or not update.message:
            return
        key = (update.effective_chat.id, update.effective_user.id)
        pending = self._awaiting_name.get(key)
        if pending is None:
            return
        action, asked_at = pending
        if asyncio.get_running_loop().time() - asked_at > ASK_NAME_SECONDS:
            self._awaiting_name.pop(key, None)
            return
        text = (update.message.text or "").strip()
        if text.lower() in CANCEL_WORDS:
            self._awaiting_name.pop(key, None)
            await update.message.reply_text("Скасовано.")
            return
        if not self._allowed(update):
            self._awaiting_name.pop(key, None)
            return
        if await self._offer_clients(update, action, text):
            self._awaiting_name.pop(key, None)

    async def _offer_clients(self, update: Update, action: str, query: str) -> bool:
        """Шукає клієнтів і показує кнопки. Повертає False, якщо нікого не знайдено."""
        found = self.service.find_clients(query)
        if not found:
            await update.message.reply_text(
                f"Не знайшов клієнта «{query}». Напишіть іншу частину імені або @username "
                f"(або «скасувати»).",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Ім'я або @username"),
                do_quote=True)
            return False
        _, _, verb = STATUS_ACTIONS[action]
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(client_button_label(c), callback_data=f"{action}:{c.chat_id}")] for c in found])
        await update.message.reply_text(f"Кого {verb.split()[0]}? Натисніть на клієнта:", reply_markup=keyboard)
        return True

    async def on_status_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._allowed(update):
            await query.answer("Це можуть робити лише в групі задач або адміни.", show_alert=True)
            return
        action, chat_id = query.data.split(":")
        status, icon, verb = STATUS_ACTIONS[action]
        client, changed = self.service.set_status(int(chat_id), status, datetime.now(timezone.utc))
        if client is None:
            await query.answer("Клієнта не знайдено.", show_alert=True)
            return
        who = client.name or "Без імені"
        if client.username:
            who += f" (@{client.username})"
        if not changed:
            already = ("вже партнер" + (f" з {fmt_date(datetime.fromisoformat(client.partner_since).astimezone(self.cfg.timezone).date())}"
                                         if client.partner_since else "")) if status == STATUS_PARTNER else "вже інвестор"
            await query.answer(already)
            await query.edit_message_text(f"ℹ️ {who} — {already}.")
            return
        self._stats_dirty = True
        await query.answer("Готово")
        by = query.from_user.full_name if query.from_user else ""
        today = fmt_date(datetime.now(self.cfg.timezone).date())
        await query.edit_message_text(f"{icon} {who} — {verb} {today}" + (f" ({by})" if by else "") + ".")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            await update.message.reply_text("Ця команда працює в групі задач або для адмінів.")
            return
        report = self._build_report()
        await update.message.reply_html(summary_text(report))
        self._stats_dirty = True

    def _build_report(self):
        since = self.db.get_setting(BOT_STARTED_KEY)
        return build_report(self.db.all_clients(include_excluded=True), self.db.all_tasks(),
                            self.db.curator_messages(self.cfg.curator_name, since),
                            datetime.now(timezone.utc), self.cfg.timezone)

    async def refresh_stats(self) -> None:
        self._stats_dirty = False
        if not self.sheets:
            return
        grid = report_grid(self._build_report(), self.cfg.timezone)
        try:
            await asyncio.to_thread(self.sheets.write_stats, grid)
        except Exception as e:  # noqa: BLE001
            log.exception("Не вдалося оновити вкладку «Статистика»")
            await self.notifier.notify(f"⚠️ Не вдалося оновити вкладку «Статистика».\n{self._explain_sheets(e)}",
                                       key="stats", cooldown=SHEETS_ERROR_COOLDOWN)

    # --- імпорт старих чатів ---
    def _can_import(self, user_id: int | None) -> bool:
        return user_id is not None and (user_id in self.cfg.admin_chat_ids or user_id in self.db.owner_ids())

    async def cmd_import(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._can_import(update.effective_user.id if update.effective_user else None):
            await update.message.reply_text("Імпорт можуть робити лише адміни або Віктор.")
            return
        await update.message.reply_text(
            "📥 Надішліть сюди файл експорту Telegram:\n"
            "• result.json (формат JSON, лише «Особисті чати»), або\n"
            "• zip-архів з ним, якщо файл більший за 20 МБ.\n\n"
            "Можна надсилати повторно — дублів не буде.")

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        doc = message.document
        user_id = update.effective_user.id if update.effective_user else None
        name = (doc.file_name or "").lower()
        if not (name.endswith(".json") or name.endswith(".zip")):
            if self._can_import(user_id):
                await message.reply_text("Для імпорту потрібен файл result.json або zip з ним.")
            return
        if not self._can_import(user_id):
            await message.reply_text("Імпорт можуть робити лише адміни або Віктор.")
            return
        if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
            mb = doc.file_size / 1_000_000
            await message.reply_text(
                f"Файл {mb:.0f} МБ, а Telegram дозволяє боту завантажити лише до 20 МБ.\n"
                f"Стисніть result.json у zip (правою кнопкою → «Стиснути»): JSON зменшується в 5–10 разів.\n"
                f"Якщо й zip завеликий — зробіть експорт частинами (у налаштуваннях експорту є період «З / До»).")
            return
        await message.reply_text("⏳ Отримав файл, завантажую чати… Це може зайняти хвилину.")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            data = await tg_file.download_as_bytearray()
            owner = next(iter(self.db.owner_ids()), None)
            export = await asyncio.to_thread(
                lambda: parse_export(load_bytes(bytes(data), name), self.cfg.timezone, owner))
        except ImportError_ as e:
            await message.reply_text(f"❌ {e}")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("Помилка імпорту")
            await message.reply_text(f"❌ Не вдалося прочитати файл: {type(e).__name__}: {e}")
            return

        started = self.db.get_setting(BOT_STARTED_KEY)
        before = datetime.fromisoformat(started) if started else None
        report = self.service.import_export(export, before, datetime.now(timezone.utc))
        self._stats_dirty = True
        oldest = (fmt_date(datetime.fromisoformat(report.oldest_contact).astimezone(self.cfg.timezone).date())
                  if report.oldest_contact else "—")
        await message.reply_text(
            "✅ Імпорт завершено\n"
            f"Особистих чатів: {report.chats} (нових клієнтів {report.new_clients}, "
            f"оновлено {report.updated_clients})\n"
            f"Повідомлень додано: {report.messages}\n"
            f"Пропущено: груп і каналів {report.skipped_groups}, ботів {report.skipped_bots}, "
            f"інших {report.skipped_other} (збережене, видалені акаунти, службові, порожні)\n"
            f"Найдавніший контакт: {oldest}\n"
            f"Готові до задач ({self.cfg.tasks_min_days}+ днів без контакту): {report.ready_for_tasks} — "
            f"щоранку по {self.cfg.tasks_per_day}.\n\n"
            "Далі: відкрийте в таблиці вкладку «Клієнти» і поставте «так» у стовпці «Не клієнт» "
            "друзям, рідним і колегам — у задачі вони не потраплятимуть.")
        if export.owner_id is None and owner is None:
            await message.reply_text(
                "⚠️ У файлі немає даних акаунта, тому я не точно знаю, які повідомлення писав Віктор. "
                "Наступного разу залиште в експорті ввімкненим «Інформація про акаунт».")
        await self._flush_sheets_once()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Привіт! Я бот куратора.\n\n"
            "/id — ваш Telegram ID\n"
            "/chatid — ID цього чату\n"
            "/tasks — задачі на сьогодні\n"
            "/partner Ім'я — перевести клієнта в партнери\n"
            "/investor Ім'я — повернути в інвестори\n"
            "/stats — статистика роботи куратора\n"
            "/import — завантажити старі чати (файл експорту)\n"
            "/status — стан бота (лише для адміна)")

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.message.reply_html(f"Ваш Telegram ID: <code>{user.id}</code>")

    async def cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        await update.message.reply_html(f"ID цього чату: <code>{chat.id}</code>")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.cfg.admin_chat_ids:
            await update.message.reply_text("Спочатку додайте змінну ADMIN_CHAT_ID у Railway (ваш ID із команди /id).")
            return
        if update.effective_chat.id not in self.cfg.admin_chat_ids:
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
        tasks_chat = self.tasks_chat_id
        tasks_line = (f"група {tasks_chat}" if tasks_chat else "група НЕ задана (TASKS_CHAT_ID) — шлю адмінам")
        today_tasks = self.db.tasks_for(self._today_local().isoformat())
        done = sum(1 for t in today_tasks if t.done_at)
        await update.message.reply_text(
            "📊 Стан бота\n"
            f"Клієнтів: {s['clients']} (нових {s['new_clients']}, старих {s['old_clients']}, "
            f"не клієнтів {s['excluded']}, партнерів {s['partners']})\n"
            f"Повідомлень у базі: {s['messages']}\n"
            f"Надіслано привітань: {s['welcomes']}\n"
            f"Привітальний текст: {welcome_line}\n"
            f"Таблиця: {sheet_line}\n"
            f"Задачі: щодня о {self.cfg.tasks_time.strftime('%H:%M')} ({self.cfg.tasks_per_day} шт., "
            f"без контакту {self.cfg.tasks_min_days}+ днів), {tasks_line}\n"
            f"Сьогодні виконано: {done} з {len(today_tasks)}\n"
            f"Заплановано повідомлень клієнтам: {len(self.db.scheduled(status='pending'))}")

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
                f"⚠️ Не вдається записати в Google Таблицю.\n{self._explain_sheets(e)}\n\n"
                f"Рядки збережені в черзі й будуть дописані, щойно запрацює.",
                key="sheets", cooldown=SHEETS_ERROR_COOLDOWN)
            return
        if self._sheets_failing:
            self._sheets_failing = False
            await self.notifier.notify("✅ Запис у Google Таблицю знову працює.")

    def _explain_sheets(self, exc: BaseException) -> str:
        email = (self.cfg.google_credentials or {}).get("client_email")
        return explain_error(exc, email)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sheet_flush_seconds)
            await self._flush_sheets_once()

    async def _post_init(self, application: Application) -> None:
        if not self.db.get_setting(BOT_STARTED_KEY):
            self.db.set_setting(BOT_STARTED_KEY, datetime.now(timezone.utc).isoformat())
        me = await application.bot.get_me()
        try:
            await application.bot.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001 — меню команд не критичне
            log.exception("Не вдалося встановити меню команд")
        lines = [f"✅ Бот @{me.username} запущено."]
        tasks_chat = self.tasks_chat_id
        lines.append(f"🕖 Пише клієнтам з {self.cfg.send_from.strftime('%H:%M')} до "
                     f"{self.cfg.send_until.strftime('%H:%M')}; нагадування через "
                     f"{self.cfg.reminder_delay_minutes} хв без відповіді.")
        lines.append(f"🗓 Задачі щодня о {self.cfg.tasks_time.strftime('%H:%M')} "
                     + (f"у групу {tasks_chat}." if tasks_chat else "— група ще не задана, надсилатиму вам."))
        if self.sheets:
            try:
                title = await asyncio.to_thread(self.sheets.check_connection)
                lines.append(f"📗 Таблиця підключена: «{title}».")
            except Exception as e:  # noqa: BLE001
                log.exception("Не вдалося підключитися до таблиці")
                self._sheets_failing = True
                lines.append(f"❌ Таблиця не підключилася.\n{self._explain_sheets(e)}")
        for w in self.cfg.warnings:
            lines.append(f"• {w}")
        await self.notifier.notify("\n".join(lines))
        if self.sheets:
            self._background.append(asyncio.create_task(self._flush_loop()))
        self._background.append(asyncio.create_task(self._scheduler_loop()))

    async def _post_shutdown(self, application: Application) -> None:
        for task in self._background:
            task.cancel()
        if self.sheets and self.sheets.pending_count():
            try:
                await asyncio.to_thread(self.sheets.flush)
            except Exception:  # noqa: BLE001
                log.exception("Не вдалося дописати таблицю перед зупинкою")
        self.db.close()

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
