"""Головна логіка: що робити з кожним повідомленням у бізнес-чатах."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Config
from .db import CLIENT_NEW, CLIENT_OLD, STATUS_INVESTOR, STATUS_PARTNER, Client, Database, Scheduled, Task
from .logic import SENDER_BOT, SENDER_CLIENT, fmt_dt, is_bonus
from .sheets import TASKS_TAB
from .schedule import in_send_window, next_send_time, reply_deadline
from .tasks import cutoff_utc, person_link, task_row
from .notify import Notifier

log = logging.getLogger(__name__)

KIND_WELCOME = "welcome"
KIND_REMINDER = "reminder"

STAGE_WELCOME_WAITING = "привітання — чекає 7:00"
STAGE_WELCOME_SENT = "привітання надіслано"
STAGE_REMINDER_SENT = "нагадування надіслано"
STAGE_REPLIED = "відповів на привітання"
STAGE_MISSED = "бот не встиг написати — задача Віктору"

TASK_STALE = "stale"
TASK_MISSED = "missed"
TASK_NO_REPLY = "no_reply"

MISSED_NOTES = {
    KIND_WELCOME: "бот не встиг надіслати привітання — напишіть самі",
    KIND_REMINDER: "не відповів на привітання, бот не встиг нагадати — зробіть касання",
}


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
                 send: SendFn, resolve_owner: OwnerFn,
                 notify_group: Callable[[str], Awaitable[object]] | None = None):
        self.notify_group = notify_group  # повідомлення в групу «Задачі куратора» (HTML)
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
        if self.is_team(msg.client_username):
            client.excluded = 1
        client.business_connection_id = msg.business_connection_id
        client.name = msg.client_name or client.name
        client.username = msg.client_username or client.username
        client.last_message_at = msg.date.isoformat()
        client.last_from = sender
        self.db.upsert_client(client)
        self._queue_rows(client, sender, msg.text, msg.date)

        if sender == self.cfg.curator_name:
            # Віктор написав клієнту — відкриті задачі по цьому чату виконано,
            # а запланованих привітання/нагадування бот уже не надсилає (Віктор веде сам).
            for task in self.db.mark_tasks_done(client.chat_id, msg.date.isoformat(), msg.text):
                self._queue_task(task)
            self.db.cancel_scheduled(client.chat_id, (KIND_WELCOME, KIND_REMINDER))
        elif sender == SENDER_CLIENT:
            # Клієнт відповів після дотику — це ознака, що дотик був справжнім.
            for task in self.db.mark_tasks_replied(client.chat_id, msg.date.isoformat()):
                self._queue_task(task)
            # Клієнт написав — Telegram знову дає боту 24 години, щоб відповісти.
            self.db.extend_deadlines(client.chat_id, reply_deadline(msg.date).isoformat())
            if (client.welcome_sent_at and not client.welcome_replied_at
                    and msg.date.isoformat() >= client.welcome_sent_at):
                await self._on_welcome_reply(client, msg)

        if wants_welcome:
            await self._start_welcome(client, msg.date)

    # --- привітання, нагадування ---
    async def _on_welcome_reply(self, client: Client, msg: IncomingMessage) -> None:
        after_reminder = bool(client.reminder_sent_at)
        client.welcome_replied_at = msg.date.isoformat()
        client.stage = STAGE_REPLIED
        self.db.upsert_client(client)
        self._queue_client(client)
        self.db.cancel_scheduled(client.chat_id, (KIND_REMINDER,))
        if self.notify_group:
            text = html.escape((msg.text or "")[:400])
            note = " (після нагадування)" if after_reminder else ""
            await self.notify_group(
                f"💬 {person_link(client.chat_id, client.name, client.username)} відповів на привітання{note} "
                f"— <b>ваша черга</b>!\n\n«{text}»")

    async def _start_welcome(self, client: Client, client_message_at: datetime) -> None:
        who = self._client_label(client)
        if not self.cfg.welcome_text:
            await self.notifier.notify(
                f"🎁 {who} написав «бонус», але привітальний текст ще не задано "
                f"(змінна WELCOME_TEXT у Railway). Відповідь не надіслано — напишіть клієнту вручну.")
            return
        now = self._now()
        due = self._next_send_time(now)
        sched_id = self.db.schedule_message(client.chat_id, client.business_connection_id, KIND_WELCOME,
                                            due.isoformat(), reply_deadline(client_message_at).isoformat(),
                                            now.isoformat())
        if due <= now:
            sched = self.db.scheduled(client.chat_id, "pending")
            await self._deliver(next(s for s in sched if s.id == sched_id), now)
        else:
            client.stage = STAGE_WELCOME_WAITING
            self.db.upsert_client(client)
            self._queue_client(client)

    def _next_send_time(self, when: datetime) -> datetime:
        return next_send_time(when, self.cfg.timezone, self.cfg.send_from, self.cfg.send_until)

    async def process_scheduled(self, now: datetime | None = None) -> None:
        """Надсилає все, чому настав час (викликається раз на 30 секунд)."""
        now = now or self._now()
        for sched in self.db.due_messages(now.isoformat()):
            try:
                await self._deliver(sched, now)
            except Exception:  # noqa: BLE001 — одне повідомлення не має зупиняти решту
                log.exception("Помилка під час відправки запланованого повідомлення %s", sched.id)

    async def _deliver(self, sched: Scheduled, now: datetime) -> None:
        client = self.db.get_client(sched.chat_id)
        if client is None:
            self.db.update_scheduled(sched.id, status="cancelled")
            return
        if sched.kind == KIND_REMINDER and client.welcome_replied_at:
            self.db.update_scheduled(sched.id, status="cancelled")
            return
        deadline = datetime.fromisoformat(sched.deadline_at)
        if now > deadline:
            await self._missed(sched, client, "минули 24 години")
            return
        if not in_send_window(now, self.cfg.timezone, self.cfg.send_from, self.cfg.send_until):
            # Наприклад, бот перезапускався і прокинувся ввечері — переносимо на ранок.
            due = self._next_send_time(now)
            if due > deadline:
                await self._missed(sched, client, "до ранку минуть 24 години")
            else:
                self.db.update_scheduled(sched.id, status="pending", due_iso=due.isoformat())
            return

        text = self.cfg.welcome_text if sched.kind == KIND_WELCOME else self.cfg.reminder_text
        try:
            message_id, date = await self.send(client.business_connection_id or sched.business_connection_id,
                                               client.chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.exception("Не вдалося надіслати %s", sched.kind)
            what = "привітання" if sched.kind == KIND_WELCOME else "нагадування"
            await self.notifier.notify(
                f"⚠️ Не вдалося надіслати {what} клієнту {self._client_label(client)}.\nПричина: {e}\n"
                f"Перевірте, що в налаштуваннях Telegram Business боту дозволено відповідати на повідомлення. "
                f"Клієнт потрапить у ранкові задачі Віктора.",
                key=f"send_fail:{sched.kind}", cooldown=600)
            await self._missed(sched, client, f"помилка Telegram: {e}")
            return

        self.db.update_scheduled(sched.id, status="sent", sent_iso=date.isoformat())
        self.db.add_message(client.chat_id, message_id, client.business_connection_id,
                            SENDER_BOT, text, date.isoformat())
        client.last_message_at = date.isoformat()
        client.last_from = SENDER_BOT
        if sched.kind == KIND_WELCOME:
            client.welcome_sent_at = date.isoformat()
            client.stage = STAGE_WELCOME_SENT
        else:
            client.reminder_sent_at = date.isoformat()
            client.stage = STAGE_REMINDER_SENT
        self.db.upsert_client(client)
        self._queue_rows(client, SENDER_BOT, text, date)

        if sched.kind == KIND_WELCOME:
            last_in = self.db.last_client_message_at(client.chat_id, SENDER_CLIENT)
            deadline = reply_deadline(datetime.fromisoformat(last_in)) if last_in else date
            due = self._next_send_time(date + timedelta(minutes=self.cfg.reminder_delay_minutes))
            status = "pending" if due <= deadline else "missed"
            self.db.schedule_message(client.chat_id, client.business_connection_id, KIND_REMINDER,
                                     due.isoformat(), deadline.isoformat(), date.isoformat(), status=status)

    async def _missed(self, sched: Scheduled, client: Client, reason: str) -> None:
        """Бот не встиг написати — клієнт піде в ранкові задачі Віктора."""
        self.db.update_scheduled(sched.id, status="missed", error=reason)
        client.stage = STAGE_MISSED
        self.db.upsert_client(client)
        self._queue_client(client)

    def is_team(self, username: str | None) -> bool:
        return bool(username) and username.lower() in self.cfg.team_usernames

    # --- щоденні задачі ---
    def create_daily_tasks(self, task_date: str, now_utc: datetime) -> list[Task]:
        """Створює задачі на день (якщо ще не створені) і повертає їх."""
        if not self.db.has_tasks(task_date):
            self.db.exclude_usernames(self.cfg.team_usernames)
            taken: set[int] = set()

            # 1) Бот не встиг надіслати привітання/нагадування.
            missed, notes = [], {}
            for sched in self.db.scheduled(status="missed"):
                client = self.db.get_client(sched.chat_id)
                self.db.update_scheduled(sched.id, status="tasked")
                if client is None or client.excluded or client.chat_id in taken:
                    continue
                if client.welcome_replied_at or self.db.curator_wrote_after(
                        client.chat_id, self.cfg.curator_name, sched.created_at):
                    continue  # клієнт уже відповів або Віктор уже написав сам
                missed.append(client)
                notes[client.chat_id] = MISSED_NOTES.get(sched.kind, "")
                taken.add(client.chat_id)
            self.db.create_tasks(task_date, missed, now_utc.isoformat(), kind=TASK_MISSED, notes=notes)

            # 2) Отримали нагадування (до сьогоднішнього ранку), але так і не відповіли.
            start_of_day = datetime.combine(datetime.fromisoformat(task_date).date(), datetime.min.time(),
                                            tzinfo=self.cfg.timezone).astimezone(timezone.utc)
            silent = []
            for client in self.db.silent_after_reminder(start_of_day.isoformat()):
                if client.chat_id in taken or self.db.has_task_kind(client.chat_id, TASK_NO_REPLY):
                    continue
                if self.db.curator_wrote_after(client.chat_id, self.cfg.curator_name, client.reminder_sent_at):
                    continue
                silent.append(client)
                taken.add(client.chat_id)
            self.db.create_tasks(task_date, silent, now_utc.isoformat(), kind=TASK_NO_REPLY,
                                 notes={c.chat_id: "не відповів ні на привітання, ні на нагадування" for c in silent})

            # 3) Давно не спілкувалися — найстаріші першими.
            cutoff = cutoff_utc(now_utc, self.cfg.tasks_min_days).isoformat()
            stale = [c for c in self.db.task_candidates(cutoff, self.cfg.tasks_per_day + len(taken))
                     if c.chat_id not in taken][:self.cfg.tasks_per_day]
            self.db.create_tasks(task_date, stale, now_utc.isoformat(), kind=TASK_STALE)
            for task in self.db.tasks_for(task_date):
                self._queue_task(task)
        return self.db.tasks_for(task_date)

    def apply_not_client_flags(self, flags: dict[int, bool]) -> int:
        """Переносить позначки «Не клієнт» з таблиці в базу. Повертає, скільки змінилося."""
        changed = 0
        for chat_id, flag in flags.items():
            client = self.db.get_client(chat_id)
            if client is None:
                continue
            excluded = flag or self.is_team(client.username)
            if self.db.set_excluded(chat_id, excluded):
                changed += 1
        return changed

    # --- партнери ---
    def find_clients(self, query: str, limit: int = 8) -> list[Client]:
        """Пошук клієнта за частиною імені, @username або ID чату. Свіжіші розмови — першими."""
        q = query.strip().lstrip("@").casefold()
        if not q:
            return []
        found = []
        for c in self.db.all_clients():
            if q.lstrip("-").isdigit() and str(c.chat_id) == q:
                return [c]
            if q in (c.name or "").casefold() or q in (c.username or "").casefold():
                found.append(c)
        found.sort(key=lambda c: c.last_message_at or "", reverse=True)
        return found[:limit]

    def set_status(self, chat_id: int, status: str, now_utc: datetime) -> tuple[Client | None, bool]:
        """Змінює статус. Повертає (клієнт, чи змінився статус)."""
        before = self.db.get_client(chat_id)
        if before is None:
            return None, False
        if before.status == status:
            return before, False
        client = self.db.set_status(chat_id, status, now_utc.isoformat())
        self._queue_client(client)
        return client, True

    def _queue_task(self, task: Task) -> None:
        if self.sheets:
            self.sheets.queue_upsert(TASKS_TAB, task_row(task, self.cfg.timezone))

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
        self._queue_client(client)

    def _queue_client(self, client: Client) -> None:
        if self.sheets:
            self.sheets.queue_client(client.chat_id, client_row(client, self.cfg.timezone))


def _fmt_iso(value: str | None, tz) -> str:
    return fmt_dt(datetime.fromisoformat(value), tz) if value else ""


STATUS_LABELS = {STATUS_INVESTOR: "Інвестор", STATUS_PARTNER: "Партнер"}


def client_row(client: Client, tz) -> list:
    """Рядок вкладки «Клієнти». «Не клієнт» записується лише в новий рядок — далі його веде людина."""
    return [
        str(client.chat_id), client.name or "", f"@{client.username}" if client.username else "",
        client.client_type, _fmt_iso(client.first_seen, tz), _fmt_iso(client.last_message_at, tz),
        client.last_from or "", _fmt_iso(client.welcome_sent_at, tz), client.stage or "",
        "так" if client.excluded else "",
        STATUS_LABELS.get(client.status, client.status), _fmt_iso(client.partner_since, tz),
    ]
