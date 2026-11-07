"""Курс інвестора: визначення тем у повідомленнях, етапи клієнта, «пропав після теми»."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .texts import COURSE_TOPICS

_YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^ ]*&)?v=|shorts/|live/|embed/))([A-Za-z0-9_-]{11})")


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    video_ids: tuple[str, ...]
    phrases: tuple[str, ...]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


TOPICS: list[Topic] = []
for _key, _label, _markers in COURSE_TOPICS:
    ids = tuple(m for m in _markers if re.fullmatch(r"[A-Za-z0-9_-]{11}", m))
    phrases = tuple(_norm(m) for m in _markers if m not in ids)
    TOPICS.append(Topic(_key, _label, ids, phrases))
TOPIC_BY_KEY = {t.key: t for t in TOPICS}

# Усі етапи по порядку. «Партнер» куратор ставить вручну (/partner).
STAGE_BONUS, STAGE_WELCOME, STAGE_WELCOME_REPLY, STAGE_PARTNER = "bonus", "welcome", "welcome_reply", "partner"
STAGES: list[tuple[str, str]] = [
    (STAGE_BONUS, "Написав «бонус»"),
    (STAGE_WELCOME, "Привітання надіслано"),
    (STAGE_WELCOME_REPLY, "Відповів на привітання"),
    *[(t.key, t.label) for t in TOPICS],
    (STAGE_PARTNER, "Партнер"),
]
STAGE_LABELS = dict(STAGES)
STAGE_INDEX = {key: i for i, (key, _) in enumerate(STAGES)}
LAST_TOPIC = TOPICS[-1].key if TOPICS else None


def detect_topics(text: str | None) -> list[str]:
    """Ключі тем, знайдених у повідомленні (за ID відео YouTube або фразою), у порядку курсу."""
    if not text:
        return []
    ids = set(_YT_ID.findall(text))
    norm = _norm(text)
    found = []
    for t in TOPICS:
        if any(v in ids for v in t.video_ids) or any(p and p in norm for p in t.phrases):
            found.append(t.key)
    return found


def stage_map(client, stored: dict[str, str]) -> dict[str, datetime]:
    """Усі етапи клієнта з датами: збережені (бонус, теми) + з полів клієнта (привітання, партнер)."""
    out: dict[str, datetime] = {k: datetime.fromisoformat(v) for k, v in stored.items() if k in STAGE_INDEX}
    if STAGE_BONUS not in out and client.client_type == "новий" and client.welcome_sent_at:
        out[STAGE_BONUS] = datetime.fromisoformat(client.welcome_sent_at)  # дані до появи етапів
    for key, value in ((STAGE_WELCOME, client.welcome_sent_at), (STAGE_WELCOME_REPLY, client.welcome_replied_at),
                       (STAGE_PARTNER, client.partner_since)):
        if value:
            out[key] = datetime.fromisoformat(value)
    return out


def current_stage(stages: dict[str, datetime]) -> str | None:
    """Найдальший досягнутий етап курсу (партнер — окремо, не враховується)."""
    reached = [k for k in stages if k != STAGE_PARTNER]
    return max(reached, key=lambda k: STAGE_INDEX[k]) if reached else None


def latest_topic(stages: dict[str, datetime]) -> str | None:
    topics = [k for k in stages if k in TOPIC_BY_KEY]
    return max(topics, key=lambda k: STAGE_INDEX[k]) if topics else None


def next_reached(stages: dict[str, datetime], key: str) -> datetime | None:
    """Коли клієнт перейшов далі: найраніший з наступних етапів (пропущені теми не заважають)."""
    later = [at for k, at in stages.items() if k != STAGE_PARTNER and STAGE_INDEX[k] > STAGE_INDEX[key]]
    return min(later) if later else None


# --- вкладки «Етапи клієнтів» і «Ефективність клієнта» ---
from zoneinfo import ZoneInfo  # noqa: E402

STAGES_TAB = "Етапи клієнтів"
COURSE_TAB = "Ефективність клієнта"
OPTIONAL_TOPICS = {"t6_2"}   # можна пропустити — тоді «➖ пропущено», а не ❌
MARK_DONE, MARK_TODO, MARK_SKIPPED = "✅", "❌", "➖ пропущено"


def _d(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%d.%m.%y")


def active_lost(stored: dict[str, str], last_client_msg: str | None) -> tuple[str, str] | None:
    """(тема, коли) — якщо клієнт «пропав» і відтоді не писав."""
    lost = [(k[5:], at) for k, at in stored.items() if k.startswith("lost:")]
    if not lost:
        return None
    key, at = max(lost, key=lambda x: x[1])
    if last_client_msg and last_client_msg > at:
        return None
    return key, at


def stages_header() -> list[str]:
    return ["Клієнт", "Username", "ID чату", "Тип", "Тем отримано", "Поточний етап", "Етап з", "Пропав після",
            "Бонус", "Привітання", "Відповів", *[t.label for t in TOPICS], "Партнер", "Посилання на чат"]


def stages_rows(clients, all_stored: dict[int, dict[str, str]], last_client: dict[int, str | None],
                tz: ZoneInfo) -> list[list]:
    """Рядок на клієнта, який почав курс: ✅ дата / ❌ / ➖ по кожному етапу."""
    from .tasks import chat_url
    rows = []
    for c in clients:
        if c.excluded:
            continue
        stored = all_stored.get(c.chat_id, {})
        st = stage_map(c, stored)
        if not any(k in st for k in (STAGE_BONUS, STAGE_WELCOME, *TOPIC_BY_KEY)):
            continue
        cur = current_stage(st)
        topics_got = sum(1 for t in TOPICS if t.key in st)
        last_key = latest_topic(st)
        lost = active_lost(stored, last_client.get(c.chat_id))

        def cell(key: str) -> str:
            if key in st:
                return f"{MARK_DONE} {_d(st[key], tz)}"
            if key in OPTIONAL_TOPICS and last_key and STAGE_INDEX[last_key] > STAGE_INDEX[key]:
                return MARK_SKIPPED
            if key in (STAGE_BONUS, STAGE_WELCOME, STAGE_WELCOME_REPLY) and c.client_type != "новий":
                return "—"  # старий клієнт не писав «бонус»
            return MARK_TODO

        rows.append((STAGE_INDEX.get(cur, -1), st.get(cur).isoformat() if cur else "", [
            c.name or "Без імені", f"@{c.username}" if c.username else "", str(c.chat_id), c.client_type,
            f"{topics_got} з {len(TOPICS)}", STAGE_LABELS.get(cur, "—") if cur else "—",
            _d(st[cur], tz) if cur else "",
            f"⚠️ {TOPIC_BY_KEY[lost[0]].label if lost[0] in TOPIC_BY_KEY else lost[0]} ({_d(datetime.fromisoformat(lost[1]), tz)})"
            if lost else "",
            cell(STAGE_BONUS), cell(STAGE_WELCOME), cell(STAGE_WELCOME_REPLY), *[cell(t.key) for t in TOPICS],
            f"{MARK_DONE} {_d(st[STAGE_PARTNER], tz)}" if STAGE_PARTNER in st else "",
            chat_url(c.chat_id, c.username),
        ]))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)  # найдалі по курсу — першими
    return [r[2] for r in rows]


# Фіксоване розташування воронки (на неї посилаються діаграми)
FUNNEL_HEADER_ROW = 4
FUNNEL_HEADER = ["Етап", "Дійшли", "% від бонусу", "% від попереднього", "Сер. днів на етапі",
                 "Медіана днів на етапі", "Сер. днів від бонусу", "Зараз на етапі", "Пропали тут (зараз)", "Пояснення"]
COURSE_WIDTH = len(FUNNEL_HEADER)


def build_course_efficiency(clients, all_stored: dict[int, dict[str, str]], last_client: dict[int, str | None],
                            now_utc: datetime, tz: ZoneInfo):
    """Воронка курсу для клієнтів, які написали «бонус»: скільки дійшли, скільки днів на етап, де пропадають."""
    import statistics
    from .efficiency import Efficiency, fmt_minutes
    from .tasks import chat_url

    rep = Efficiency(updated_at=now_utc)
    rows, styles = rep.rows, rep.styles

    def add(row: list, style: str = "") -> None:
        if style:
            styles.append((len(rows), style))
        rows.append(row + [""] * (COURSE_WIDTH - len(row)))

    active = [c for c in clients if not c.excluded]
    maps = {c.chat_id: stage_map(c, all_stored.get(c.chat_id, {})) for c in active}
    cohort = [c for c in active if STAGE_BONUS in maps[c.chat_id]]
    lost_now = {c.chat_id: active_lost(all_stored.get(c.chat_id, {}), last_client.get(c.chat_id)) for c in active}

    add(["🎓 Ефективність клієнта — курс інвестора"], "title")
    add([f"Оновлено: {now_utc.astimezone(tz).strftime('%d.%m.%Y %H:%M')}. Воронка — для тих, хто написав «бонус». "
         f"Тема пройдена, коли надіслано наступну."], "note")
    add([])
    add(["📊 Воронка курсу"], "section")
    add(FUNNEL_HEADER, "colheader")
    assert len(rows) == FUNNEL_HEADER_ROW + 1
    bonus_n = sum(1 for c in cohort)
    prev_n = None
    notes = {STAGE_BONUS: "Усі, хто написав «бонус»", STAGE_WELCOME_REPLY: "Почали діалог",
             STAGE_PARTNER: "Ставить куратор вручну (/partner)",
             "t6_2": "Може бути пропущена — тоді не рахується як відпад"}
    for key, label in STAGES:
        reached = [c for c in cohort if key in maps[c.chat_id]]
        n = len(reached)
        durations, from_bonus = [], []
        for c in reached:
            st = maps[c.chat_id]
            nxt = next_reached(st, key) if key != STAGE_PARTNER else None
            if nxt:
                durations.append((nxt - st[key]).total_seconds() / 86400)
            if STAGE_BONUS in st:
                from_bonus.append((st[key] - st[STAGE_BONUS]).total_seconds() / 86400)
        now_here = sum(1 for c in cohort if current_stage(maps[c.chat_id]) == key)
        lost_here = sum(1 for c in cohort if lost_now[c.chat_id] and lost_now[c.chat_id][0] == key)
        add([label, n, f"{round(100 * n / bonus_n)}%" if bonus_n else "—",
             f"{round(100 * n / prev_n)}%" if prev_n else "—",
             round(statistics.mean(durations), 1) if durations else "",
             round(statistics.median(durations), 1) if durations else "",
             round(statistics.mean(from_bonus), 1) if from_bonus and key != STAGE_BONUS else "",
             now_here if key != STAGE_PARTNER else "", lost_here if key in TOPIC_BY_KEY else "",
             notes.get(key, "")])
        if key not in OPTIONAL_TOPICS:
            prev_n = n

    # Де пропадають
    add([])
    add(["🕳 Де пропадають (зараз мовчать після теми)"], "section")
    by_topic: dict[str, int] = {}
    for c in active:
        lost = lost_now[c.chat_id]
        if lost:
            by_topic[lost[0]] = by_topic.get(lost[0], 0) + 1
    if by_topic:
        worst = max(by_topic, key=by_topic.get)
        add(["Найбільше пропадають після", TOPIC_BY_KEY[worst].label if worst in TOPIC_BY_KEY else worst,
             by_topic[worst], "", "", "", "", "", "", "Варто переглянути цю тему або питання до неї"])
    add(["Клієнт", "Username", "Пропав після", "Тема надіслана", "Від теми минуло", "Посилання на чат", "", "", "",
         "Тип"], "colheader")
    lost_list = []
    for c in active:
        lost = lost_now[c.chat_id]
        if not lost:
            continue
        st = maps[c.chat_id]
        topic_at = st.get(lost[0])
        lost_list.append((topic_at or now_utc, c, lost))
    if not lost_list:
        add(["Зараз ніхто не пропав 🎉"])
    for topic_at, c, lost in sorted(lost_list, key=lambda x: x[0]):
        silent = fmt_minutes((now_utc - topic_at).total_seconds() / 60) if topic_at else ""
        add([c.name or "Без імені", f"@{c.username}" if c.username else "",
             TOPIC_BY_KEY[lost[0]].label if lost[0] in TOPIC_BY_KEY else lost[0],
             _d(topic_at, tz) if topic_at else "", silent, chat_url(c.chat_id, c.username), "", "", "",
             "новий (бонус)" if c.client_type == "новий" else "старий"])

    # Старі клієнти на курсі
    old_on_course = [c for c in active if c not in cohort and any(k in maps[c.chat_id] for k in TOPIC_BY_KEY)]
    add([])
    add(["👥 Старі клієнти, які теж отримують теми"], "section")
    add(["Кількість", len(old_on_course), "", "", "", "", "", "", "",
         "Не входять у воронку вище; їхні етапи — у вкладці «Етапи клієнтів»"])
    return rep


def course_chart_specs(sheet_id: int) -> list[tuple[str, dict, int]]:
    from .stats import _basic
    n = len(STAGES)
    return [
        ("🎓 Воронка курсу: скільки дійшли до кожного етапу",
         {"basicChart": _basic(sheet_id, FUNNEL_HEADER_ROW, n, "COLUMN", [1], "Людей", "Етап")}, 0),
        ("⏳ Середня кількість днів на етапі",
         {"basicChart": _basic(sheet_id, FUNNEL_HEADER_ROW, n, "COLUMN", [4], "Днів", "Етап")}, 18),
        ("🕳 Де пропадають (зараз мовчать після теми)",
         {"basicChart": _basic(sheet_id, FUNNEL_HEADER_ROW, n, "COLUMN", [8], "Людей", "Етап")}, 36),
    ]



# --- діагностика: які відео Віктор надсилав і чи бот їх упізнає ---
def course_check(curator_texts: list[tuple[int, str, str]], tz: ZoneInfo, top: int = 15) -> str:
    """Звіт для /coursecheck. curator_texts: (chat_id, дата, текст) усіх повідомлень куратора."""
    known_ids = {v for t in TOPICS for v in t.video_ids}
    total = with_yt = recognized = 0
    unknown: dict[str, list] = {}
    clients_with_topics: set[int] = set()
    for chat_id, date, text in curator_texts:
        total += 1
        ids = _YT_ID.findall(text or "")
        if detect_topics(text):
            recognized += 1
            clients_with_topics.add(chat_id)
        if not ids:
            continue
        with_yt += 1
        for vid in ids:
            if vid in known_ids:
                continue
            info = unknown.setdefault(vid, [0, set(), date, date, (text or "").strip()])
            info[0] += 1
            info[1].add(chat_id)
            info[2], info[3] = min(info[2], date), max(info[3], date)
    lines = [
        "🔎 <b>Перевірка тем курсу в переписках</b>",
        f"Повідомлень Віктора в базі: {total}",
        f"…з посиланням на YouTube: {with_yt}",
        f"…упізнано як тему курсу: {recognized} (у {len(clients_with_topics)} клієнтів)",
        "",
    ]
    if not unknown:
        lines.append("Невідомих відео немає ✅")
    else:
        lines.append(f"<b>Невідомі відео</b> (бот не знає, яка це тема) — топ {min(top, len(unknown))} з {len(unknown)}:")
        for vid, (n, chats, first, last, sample) in sorted(unknown.items(), key=lambda x: -x[1][0])[:top]:
            d1 = datetime.fromisoformat(first).astimezone(tz).strftime("%d.%m.%y")
            d2 = datetime.fromisoformat(last).astimezone(tz).strftime("%d.%m.%y")
            snippet = " ".join(sample.split())[:70]
            lines.append(f"• <code>{vid}</code> — {n} раз, {len(chats)} клієнтам, {d1}–{d2}\n   «{_html(snippet)}»")
        lines.append("")
        lines.append("Напишіть, яка тема кожне з цих відео — я додам їх, і бот перерахує етапи.")
    return "\n".join(lines)


def _html(text: str) -> str:
    import html
    return html.escape(text)
