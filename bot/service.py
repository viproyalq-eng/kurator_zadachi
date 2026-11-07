"""Головна логіка: що робити з кожним повідомленням у бізнес-чатах."""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Config
from .db import CLIENT_NEW, CLIENT_OLD, STATUS_INVESTOR, STATUS_PARTNER, Client, Database, Scheduled, Task
from .logic import SENDER_BOT, SENDER_CLIENT, fmt_dt, is_bonus
from .course import STAGE_BONUS, TOPIC_BY_KEY, detect_topics, latest_topic
from .schedule import in_send_window, next_send_time, reply_deadline
from .texts import LOST_AFTER_DAYS
from .tasks import chat_url, cutoff_utc, person_link, task_row
from .notify import Notifier

log = logging.getLogger(__name__)

KIND_WELCOME = "welcome"
KIND_REMINDER = "reminder"

STAGE_WELCOME_WAITING = "привітання — чекає 7:00"
STAGE_WELCOME_SENT = "привітання надіслано"
STAGE_REMINDER_SENT = "нагадування надіслано"
STAGE_REPLIED = "відповів на привітання"
STAGE_MISSED = "бот не встиг написати — задача Віктору"

ROUND_NO_KEY = "round_no"
ROUND_START_KEY = "round_started_at"
ROUND_HISTORY_KEY = "rounds_history"

TASK_STALE = "stale"
TASK_LOST = "lost"
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


_CHAT_LINK = re.compile(r"(?:web\.telegram\.org/[a-z]/#|tg://user\?id=|tg://openmessage\?user_id=)(-?\d+)",
                        re.IGNORECASE)
_USERNAME_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?$", re.IGNORECASE)


def _chat_id_from_link(token: str) -> int | None:
    m = _CHAT_LINK.search(token)
    return int(m.group(1)) if m else None


def _username_from_link(token: str) -> str | None:
    if token.startswith("@") and len(token) > 1:
        return token[1:]
    m = _USERNAME_LINK.match(token)
    return m.group(1) if m else None


@dataclass
class ImportReport:
    chats: int = 0
    new_clients: int = 0
    updated_clients: int = 0
    messages: int = 0
    skipped_groups: int = 0
    skipped_bots: int = 0
    skipped_other: int = 0
    oldest_contact: str | None = None
    ready_for_tasks: int = 0


class BotService:
    def __init__(self, cfg: Config, db: Database, sheets, notifier: Notifier,
                 send: SendFn, resolve_owner: OwnerFn,
                 notify_group: Callable[[str], Awaitable[object]] | None = None):
        self.notify_group = notify_group  # повідомлення в групу «Задачі куратора» (HTML)
        self.tasks_dirty = False          # треба переписати вкладки задач у таблиці
        self.stages_dirty = False         # треба переписати вкладки етапів курсу
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
            topics = detect_topics(msg.text)
            if topics:
                self._apply_topics(client, topics, msg.date)
            if self.db.get_setting(ROUND_START_KEY):
                await self.maybe_finish_round(self._now())
        elif sender == SENDER_CLIENT:
            # Клієнт відповів після дотику — це ознака, що дотик був справжнім.
            for task in self.db.mark_tasks_replied(client.chat_id, msg.date.isoformat()):
                self._queue_task(task)
            # Клієнт написав — Telegram знову дає боту 24 години, щоб відповісти.
            self.db.extend_deadlines(client.chat_id, reply_deadline(msg.date).isoformat())
            if (client.welcome_sent_at and not client.welcome_replied_at
                    and msg.date.isoformat() >= client.welcome_sent_at):
                await self._on_welcome_reply(client, msg)
            if (client.cold_since and self.notify_group
                    and self.db.client_messages_after(client.chat_id, SENDER_CLIENT, client.cold_since) == 1):
                await self.notify_group(
                    f"❄️ Холодний клієнт {person_link(client.chat_id, client.name, client.username)} написав сам:\n"
                    f"«{html.escape((msg.text or '')[:400])}»\n\nЯкщо варто знову вести — /warm")

        if wants_welcome:
            self.db.record_stage(client.chat_id, STAGE_BONUS, msg.date.isoformat())
            self.stages_dirty = True
            await self._start_welcome(client, msg.date)

    # --- курс інвестора: етапи ---
    def _apply_topics(self, client: Client, keys: list[str], at: datetime) -> None:
        for key in keys:
            self.db.record_stage(client.chat_id, key, at.isoformat())
        self._refresh_client_stage(client)
        self.stages_dirty = True

    def _refresh_client_stage(self, client: Client) -> bool:
        """Етап у «Клієнтах» = найдальша отримана тема (якщо вона новіша за поточний етап)."""
        stored = self.db.stages_of(client.chat_id)
        key = latest_topic({k: datetime.fromisoformat(v) for k, v in stored.items() if k in TOPIC_BY_KEY})
        if not key:
            return False
        label, at = TOPIC_BY_KEY[key].label, stored[key]
        if client.stage == label and client.stage_at == at:
            return False
        if client.stage_at and client.stage_at > at and client.stage not in TOPIC_LABELS:
            return False  # пізніший не-курсовий етап (напр. відповів на привітання) — не чіпаємо
        client.stage, client.stage_at = label, at
        self.db.upsert_client(client)
        self._queue_client(client)
        return True

    def rescan_course_history(self) -> int:
        """Шукає теми курсу в усій збереженій історії (і імпортованій). Повертає, скільки клієнтів оновлено."""
        touched: set[int] = set()
        for chat_id, date, text in self.db.curator_texts(self.cfg.curator_name):
            low = text.lower()
            if "youtu" not in low and not any(p for t in TOPIC_BY_KEY.values() for p in t.phrases if p in low):
                continue
            for key in detect_topics(text):
                self.db.record_stage(chat_id, key, date)
                touched.add(chat_id)
        updated = 0
        for chat_id in touched:
            client = self.db.get_client(chat_id)
            if client and self._refresh_client_stage(client):
                updated += 1
        if touched:
            self.stages_dirty = True
        return updated

    def _ball_with_client_since(self, chat_id: int) -> datetime | None:
        """Якщо останнім писав Віктор (або бот) — коли саме. Інакше None (чекаємо Віктора)."""
        last_out = self.db.last_message_at_by(chat_id, (self.cfg.curator_name, SENDER_BOT))
        last_in = self.db.last_message_at_by(chat_id, (SENDER_CLIENT,))
        if not last_out or (last_in and last_in > last_out):
            return None
        return datetime.fromisoformat(last_out)

    async def check_lost(self, now: datetime | None = None) -> int:
        """«Пропав після теми N»: після теми останнім писав Віктор, а клієнт мовчить LOST_AFTER_DAYS днів."""
        now = now or self._now()
        alerted = 0
        for chat_id, stored in self.db.all_stages().items():
            client = self.db.get_client(chat_id)
            if client is None or client.excluded or client.cold_since or client.status == STATUS_PARTNER:
                continue
            key = latest_topic({k: datetime.fromisoformat(v) for k, v in stored.items() if k in TOPIC_BY_KEY})
            if not key or f"lost:{key}" in stored:
                continue
            since = self._ball_with_client_since(chat_id)
            if since is None or since.isoformat() < stored[key] or now - since < timedelta(days=LOST_AFTER_DAYS):
                continue
            self.db.record_stage(chat_id, f"lost:{key}", now.isoformat())
            self.stages_dirty = True
            alerted += 1
            if self.notify_group:
                topic_at = datetime.fromisoformat(stored[key]).astimezone(self.cfg.timezone).strftime("%d.%m")
                await self.notify_group(
                    f"⚠️ {person_link(client.chat_id, client.name, client.username)} пропав після "
                    f"<b>{TOPIC_BY_KEY[key].label}</b> (надіслана {topic_at}) — мовчить "
                    f"{(now - since).days} дн.\nТреба втрутитися.")
        return alerted

    def _lost_candidates(self) -> tuple[list[Client], dict[int, str]]:
        """Хто «пропав» і ще не отримав за це задачу; Віктор після сповіщення не писав, клієнт мовчить."""
        clients, notes = [], {}
        for chat_id, stored in self.db.all_stages().items():
            for stage, at in stored.items():
                if not stage.startswith("lost:") or f"lost_task:{stage[5:]}" in stored:
                    continue
                client = self.db.get_client(chat_id)
                if client is None or client.excluded or client.cold_since or client.status == STATUS_PARTNER:
                    continue
                last_in = self.db.last_message_at_by(chat_id, (SENDER_CLIENT,))
                if (last_in and last_in > at) or self.db.curator_wrote_after(chat_id, self.cfg.curator_name, at):
                    continue  # повернувся або Віктор уже втрутився
                topic = TOPIC_BY_KEY.get(stage[5:])
                clients.append(client)
                notes[chat_id] = f"пропав після «{topic.label if topic else stage[5:]}» — втрутитися"
                self.db.record_stage(chat_id, f"lost_task:{stage[5:]}", at)
        return clients, notes

    # --- привітання, нагадування ---
    async def _on_welcome_reply(self, client: Client, msg: IncomingMessage) -> None:
        after_reminder = bool(client.reminder_sent_at)
        client.welcome_replied_at = msg.date.isoformat()
        _set_stage(client, STAGE_REPLIED, msg.date)
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
            _set_stage(client, STAGE_WELCOME_WAITING, now)
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
        if (sched.kind == KIND_REMINDER and client.welcome_replied_at) or client.cold_since:
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
            _set_stage(client, STAGE_WELCOME_SENT, date)
        else:
            client.reminder_sent_at = date.isoformat()
            _set_stage(client, STAGE_REMINDER_SENT, date)
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
        _set_stage(client, STAGE_MISSED, self._now())
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
                if client is None or client.excluded or client.cold_since or client.chat_id in taken:
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

            # 3) Пропали на курсі (після теми мовчать) — втрутитися.
            lost, lost_notes = self._lost_candidates()
            lost = [c for c in lost if c.chat_id not in taken]
            taken.update(c.chat_id for c in lost)
            self.db.create_tasks(task_date, lost, now_utc.isoformat(), kind=TASK_LOST, notes=lost_notes)

            # 4) Коло обходу: ще не пройдені в цьому колі, найдавніший контакт — першими.
            cutoff = cutoff_utc(now_utc, self.cfg.tasks_min_days).isoformat()
            _, round_start = self.round_info(now_utc)
            stale = [c for c in self.db.round_candidates(cutoff, round_start, self.cfg.curator_name,
                                                         self.cfg.tasks_per_day + len(taken))
                     if c.chat_id not in taken][:self.cfg.tasks_per_day]
            self.db.create_tasks(task_date, stale, now_utc.isoformat(), kind=TASK_STALE)
            for task in self.db.tasks_for(task_date):
                self._queue_task(task)
        return self.db.tasks_for(task_date)

    # --- кола обходу ---
    def round_info(self, now_utc: datetime) -> tuple[int, str]:
        """(номер кола, коли почалося). Перше коло починається при першому зверненні."""
        number = self.db.get_setting(ROUND_NO_KEY)
        start = self.db.get_setting(ROUND_START_KEY)
        if not number or not start:
            number, start = "1", now_utc.isoformat()
            self.db.set_setting(ROUND_NO_KEY, number)
            self.db.set_setting(ROUND_START_KEY, start)
        return int(number), start

    def round_progress(self, now_utc: datetime) -> tuple[int, int, int]:
        """(номер кола, пройдено, усього)."""
        number, start = self.round_info(now_utc)
        covered, total = self.db.round_progress(start, self.cfg.curator_name)
        return number, covered, total

    async def maybe_finish_round(self, now_utc: datetime) -> bool:
        """Якщо в колі пройдено всіх — повідомляє групу і починає нове коло."""
        number, start = self.round_info(now_utc)
        covered, total = self.db.round_progress(start, self.cfg.curator_name)
        if total == 0 or covered < total:
            return False
        days = max(1, (now_utc - datetime.fromisoformat(start)).days)
        history = json.loads(self.db.get_setting(ROUND_HISTORY_KEY) or "[]")
        history.append({"no": number, "start": start, "end": now_utc.isoformat(), "total": total})
        self.db.set_setting(ROUND_HISTORY_KEY, json.dumps(history))
        self.db.set_setting(ROUND_NO_KEY, str(number + 1))
        self.db.set_setting(ROUND_START_KEY, now_utc.isoformat())
        if self.notify_group:
            await self.notify_group(
                f"🎉 <b>Усіх клієнтів пройдено!</b>\n"
                f"Коло №{number} завершено: {total} клієнтів за {days} дн.\n\n"
                f"Починаємо коло №{number + 1} — знову по {self.cfg.tasks_per_day} на день, від найдавнішого "
                f"контакту (кому писали менше {self.cfg.tasks_min_days} днів тому — трохи пізніше). "
                f"Партнерів і холодних у колі немає.")
        return True

    def efficiency(self, now_utc: datetime):
        """Усі дані для вкладки «Ефективність куратора»."""
        from .efficiency import LiveMessage, RoundState, build_efficiency
        from .stats import BOT_STARTED_KEY
        since = self.db.get_setting(BOT_STARTED_KEY)
        live = [LiveMessage(cid, sender, datetime.fromisoformat(d), text or "")
                for cid, sender, d, text in self.db.live_messages(since)]
        number, covered, total = self.round_progress(now_utc)
        _, start = self.round_info(now_utc)
        rounds = RoundState(number, datetime.fromisoformat(start), covered, total, self.cfg.tasks_per_day,
                            json.loads(self.db.get_setting(ROUND_HISTORY_KEY) or "[]"))
        alive = self.db.alive_clients(SENDER_CLIENT, (now_utc - timedelta(days=30)).isoformat())
        return build_efficiency(self.db.all_clients(include_excluded=True), self.db.all_tasks(), live, alive,
                                rounds, now_utc, self.cfg.timezone, self.cfg.curator_name, SENDER_CLIENT)

    def course_tabs(self, now_utc: datetime):
        """(заголовок і рядки «Етапів клієнтів», дашборд «Ефективність клієнта»)."""
        from .course import build_course_efficiency, stages_header, stages_rows
        clients = self.db.all_clients(include_excluded=True)
        stored = self.db.all_stages()
        last_client = {c.chat_id: self.db.last_message_at_by(c.chat_id, (SENDER_CLIENT,))
                       for c in clients if c.chat_id in stored or c.client_type == CLIENT_NEW}
        self.stages_dirty = False
        return ((stages_header(), stages_rows(clients, stored, last_client, self.cfg.timezone)),
                build_course_efficiency(clients, stored, last_client, now_utc, self.cfg.timezone))

    def trends(self, now_utc: datetime):
        """Тренди по днях і тижнях для «Діаграм ефективності»."""
        from .efficiency import LiveMessage, build_trends
        from .stats import BOT_STARTED_KEY
        since = self.db.get_setting(BOT_STARTED_KEY)
        live = [LiveMessage(cid, sender, datetime.fromisoformat(d), text or "")
                for cid, sender, d, text in self.db.live_messages(since)]
        _, start = self.round_info(now_utc)
        return build_trends(self.db.all_clients(include_excluded=True), self.db.all_tasks(), live,
                            datetime.fromisoformat(start), now_utc, self.cfg.timezone,
                            self.cfg.curator_name, SENDER_CLIENT)

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

    # --- імпорт старих чатів ---
    def import_export(self, export, before: datetime | None, now: datetime) -> "ImportReport":
        """Заносить чати з експорту Telegram у базу і таблицю.
        Повідомлення, новіші за запуск бота (before), пропускаємо — їх бот уже записав сам."""
        report = ImportReport(skipped_groups=export.skipped_groups, skipped_bots=export.skipped_bots,
                              skipped_other=export.skipped_other)
        self.db.exclude_usernames(self.cfg.team_usernames)
        for chat in export.chats:
            msgs = [m for m in chat.messages if before is None or m.date < before]
            if not msgs:
                continue
            msgs.sort(key=lambda m: m.date)
            rows = [(chat.chat_id, m.message_id, None, self.cfg.curator_name if m.from_owner else SENDER_CLIENT,
                     m.text, m.date.isoformat()) for m in msgs]
            report.messages += self.db.add_messages_bulk(rows)
            first, last = msgs[0], msgs[-1]
            last_from = self.cfg.curator_name if last.from_owner else SENDER_CLIENT
            client = self.db.get_client(chat.chat_id)
            if client is None:
                client = Client(chat_id=chat.chat_id, business_connection_id=None, name=chat.name, username=None,
                                client_type=CLIENT_OLD, first_seen=first.date.isoformat(),
                                last_message_at=last.date.isoformat(), last_from=last_from,
                                welcome_sent_at=None, stage=None)
                report.new_clients += 1
            else:
                client.name = client.name or chat.name
                if first.date.isoformat() < client.first_seen:
                    client.first_seen = first.date.isoformat()
                if not client.last_message_at or last.date.isoformat() > client.last_message_at:
                    client.last_message_at = last.date.isoformat()
                    client.last_from = last_from
                report.updated_clients += 1
            self.db.upsert_client(client)
            self._queue_client(self.db.get_client(chat.chat_id))
            report.chats += 1
            if report.oldest_contact is None or client.last_message_at < report.oldest_contact:
                report.oldest_contact = client.last_message_at
        cutoff = cutoff_utc(now, self.cfg.tasks_min_days).isoformat()
        _, round_start = self.round_info(now)
        report.ready_for_tasks = len(self.db.round_candidates(cutoff, round_start, self.cfg.curator_name, 10 ** 9))
        return report

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

    def set_cold(self, chat_id: int, cold: bool, now_utc: datetime) -> tuple[Client | None, bool]:
        """Перенести в «холодні» (або повернути). Повертає (клієнт, чи змінилося)."""
        before = self.db.get_client(chat_id)
        if before is None:
            return None, False
        if bool(before.cold_since) == cold:
            return before, False
        client = self.db.set_cold(chat_id, now_utc.isoformat() if cold else None)
        if cold:
            # холодним бот нічого не надсилає
            self.db.cancel_scheduled(chat_id, (KIND_WELCOME, KIND_REMINDER))
        self._queue_client(client)
        return client, True

    def resolve_tokens(self, text: str, today_tasks: list[Task]) -> tuple[list[Client], list[str], str]:
        """Розбирає, кого мав на увазі куратор. Розуміє:
        - номер зі сьогоднішнього списку задач (3, 7);
        - @username або посилання t.me/username;
        - посилання на чат: web.telegram.org/a/#123456789, tg://user?id=…, або сам ID чату;
        - решта слів — ім'я, як людина підписана (пошук → кнопки).
        Повертає (точно знайдені, не знайдені, текст для пошуку за іменем)."""
        from .tasks import ordered_tasks
        ordered = ordered_tasks(today_tasks)
        found: list[Client] = []
        missing: list[str] = []
        words: list[str] = []
        for token in re.split(r"[\s,;]+", text.strip()):
            if not token:
                continue
            chat_id = _chat_id_from_link(token)
            username = None if chat_id is not None else _username_from_link(token)
            if chat_id is not None:
                client = self.db.get_client(chat_id)
                (found.append(client) if client else missing.append(token))
            elif username:
                client = self.db.find_by_username(username)
                (found.append(client) if client else missing.append("@" + username))
            elif token.isdigit():
                n = int(token)
                if n <= 1000:
                    if 1 <= n <= len(ordered) and (client := self.db.get_client(ordered[n - 1].chat_id)):
                        found.append(client)
                    else:
                        missing.append(f"№{n}")
                else:  # великі числа — це ID чату
                    client = self.db.get_client(n)
                    (found.append(client) if client else missing.append(token))
            else:
                words.append(token)
        unique = list({c.chat_id: c for c in found}.values())
        return unique, missing, " ".join(words)

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
        # Вкладки «Задачі» і «Виконані задачі» переписуються цілком (див. task_lists) —
        # тут лише позначаємо, що їх треба оновити найближчим часом.
        self.tasks_dirty = True

    def task_lists(self) -> tuple[list[list], list[list]]:
        """Рядки для вкладок: (невиконані, виконані)."""
        tz = self.cfg.timezone
        return ([task_row(t, tz) for t in self.db.tasks_by_state(done=False)],
                [task_row(t, tz) for t in self.db.tasks_by_state(done=True)])

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
        chat_url(client.chat_id, client.username),
        _fmt_iso(client.cold_since, tz),
        _fmt_iso(client.stage_at, tz),
    ]


def cold_row(client: Client, tz) -> list:
    """Рядок вкладки «Холодні»."""
    return [client.name or "", f"@{client.username}" if client.username else "", str(client.chat_id),
            _fmt_iso(client.cold_since, tz), _fmt_iso(client.last_message_at, tz), client.last_from or "",
            STATUS_LABELS.get(client.status, client.status), chat_url(client.chat_id, client.username)]


def partner_row(client: Client, tz) -> list:
    """Рядок вкладки «Партнери»."""
    return [client.name or "", f"@{client.username}" if client.username else "", str(client.chat_id),
            _fmt_iso(client.partner_since, tz), client.client_type, _fmt_iso(client.first_seen, tz),
            _fmt_iso(client.last_message_at, tz), client.last_from or "", _fmt_iso(client.cold_since, tz),
            chat_url(client.chat_id, client.username)]


TOPIC_LABELS = {t.label for t in TOPIC_BY_KEY.values()}


def _set_stage(client: Client, label: str, at: datetime) -> None:
    client.stage = label
    client.stage_at = at.isoformat()
