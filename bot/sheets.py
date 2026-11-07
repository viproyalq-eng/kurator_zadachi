"""Запис у Google Таблицю.

Рядки не пишуться в таблицю миттєво: вони збираються в чергу і раз на кілька
секунд відправляються однією пачкою. Так ми не впираємося в ліміти Google,
а якщо Google тимчасово недоступний — рядки чекають і відправляються пізніше.

Вкладки:
- «Повідомлення» — кожне повідомлення окремим рядком (лише додаються);
- «Клієнти» — один рядок на клієнта (оновлюється);
- «Задачі» — щоденні задачі куратору (оновлюється, коли задачу виконано).

Стовпець «Не клієнт» у вкладці «Клієнти» заповнює людина (Віктор): бот його
лише читає і при оновленні рядка не перезаписує.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

MESSAGES_TAB = "Повідомлення"
CLIENTS_TAB = "Клієнти"
TASKS_TAB = "Задачі"
DONE_TASKS_TAB = "Виконані задачі"

MESSAGES_HEADER = ["Дата", "Клієнт", "Username", "ID чату", "Хто написав", "Текст"]
CLIENTS_HEADER = ["ID чату", "Клієнт", "Username", "Тип", "Перший контакт",
                  "Останнє повідомлення", "Хто писав останнім", "Привітання надіслано", "Етап",
                  "Не клієнт", "Статус", "Партнер з", "Посилання на чат", "Холодний з"]
NOT_CLIENT_COLUMN = "Не клієнт"
NOT_CLIENT_INDEX = CLIENTS_HEADER.index(NOT_CLIENT_COLUMN)  # J — заповнює людина, бот не перезаписує
TASKS_HEADER = ["Дата задачі", "Клієнт", "Username", "ID чату", "Останній контакт",
                "Днів без контакту", "Виконано", "Текст дотику", "Клієнт відповів", "Тип задачі", "Посилання на чат"]

PARTNERS_TAB = "Партнери"
PARTNERS_HEADER = ["Клієнт", "Username", "ID чату", "Партнер з", "Тип (як прийшов)", "Перший контакт",
                   "Останній контакт", "Хто писав останнім", "Холодний з", "Посилання на чат"]
COLD_TAB = "Холодні"
COLD_HEADER = ["Клієнт", "Username", "ID чату", "Холодний з", "Останній контакт", "Хто писав останнім",
               "Статус", "Посилання на чат"]

TRUTHY = {"так", "да", "yes", "y", "1", "x", "х", "+", "true", "✓", "✔", "✅", "v", "не клієнт"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass(frozen=True)
class Tab:
    title: str
    header: list
    key_cols: tuple            # стовпці, з яких складається «ключ» рядка
    human_cols: tuple = ()     # стовпці, які заповнює людина: при оновленні бот їх не чіпає

    def update_segments(self) -> list[tuple[int, int]]:
        """Суцільні діапазони стовпців [від, до) для оновлення, без «людських» стовпців."""
        segments, start = [], None
        for i in range(len(self.header) + 1):
            bot_col = i < len(self.header) and i not in self.human_cols
            if bot_col and start is None:
                start = i
            elif not bot_col and start is not None:
                segments.append((start, i))
                start = None
        return segments


CLIENTS = Tab(CLIENTS_TAB, CLIENTS_HEADER, (0,), (NOT_CLIENT_INDEX,))
TASKS = Tab(TASKS_TAB, TASKS_HEADER, (0, 3))
UPSERT_TABS = {CLIENTS.title: CLIENTS, TASKS.title: TASKS}


def is_truthy(value) -> bool:
    return str(value).strip().lower() in TRUTHY


def _api_message(exc: BaseException) -> str:
    """Дістає текст відповіді Google з помилки (бібліотека часто ховає його всередині)."""
    for e in (exc, exc.__cause__, exc.__context__):
        if e is None:
            continue
        response = getattr(e, "response", None)
        if response is not None:
            try:
                return str(response.json().get("error", {}).get("message", "")) or str(e)
            except Exception:  # noqa: BLE001
                return getattr(response, "text", "") or str(e)
    return str(exc)


def explain_error(exc: BaseException, client_email: str | None) -> str:
    """Зрозуміле пояснення помилки Google Таблиці для адміна."""
    name = type(exc).__name__
    detail = _api_message(exc)
    low = detail.lower()
    email = client_email or "email сервісного акаунта (поле client_email у JSON)"
    if "has not been used" in low or "is disabled" in low or "service_disabled" in low:
        api = "Google Drive API" if "drive" in low else "Google Sheets API"
        return (f"У Google Cloud не ввімкнено {api}. Відкрийте console.cloud.google.com → той самий проєкт, "
                f"що й сервісний акаунт → «APIs & Services» → «Enable APIs» → знайдіть {api} → Enable. "
                f"Через 2–3 хвилини бот підключиться сам.")
    if name == "PermissionError" or "permission" in low or "403" in detail:
        return (f"Немає доступу до таблиці. Відкрийте таблицю → «Поділитися» (Share) → впишіть\n{email}\n"
                f"→ права «Редактор» → «Надіслати». Бот підключиться сам протягом хвилини.")
    if name == "SpreadsheetNotFound" or "not found" in low or "404" in detail:
        return ("Таблицю не знайдено. Перевірте змінну GOOGLE_SHEET_ID: це частина посилання між /d/ і /edit, "
                "без пробілів і зайвих символів.")
    if "invalid_grant" in low or "invalid jwt" in low or "private key" in low:
        return ("Google не приймає ключ сервісного акаунта. Перевірте GOOGLE_CREDENTIALS_JSON: вставлено весь "
                "JSON без змін; якщо ключ видаляли в Google Cloud — створіть новий.")
    return f"{name}: {detail}" if detail else name


def default_client_factory(credentials: dict):
    import gspread
    return gspread.service_account_from_dict(credentials, scopes=SCOPES)


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _row_key(tab: Tab, row: list) -> str:
    return "|".join(str(row[i]).strip() if i < len(row) else "" for i in tab.key_cols)


class SheetWriter:
    def __init__(self, sheet_id: str, credentials: dict,
                 client_factory: Callable[[dict], object] = default_client_factory):
        self.sheet_id = sheet_id
        self.credentials = credentials
        self.client_factory = client_factory
        self._lock = threading.Lock()
        self._pending_messages: list[list] = []
        # вкладка -> {ключ: (рядок для оновлення, рядок для нового запису)}
        self._pending_upserts: dict[str, dict[str, tuple[list, list]]] = {}
        self._spreadsheet = None
        self._row_index: dict[str, dict[str, int]] = {}

    # --- черга ---
    def queue_message(self, row: list) -> None:
        with self._lock:
            self._pending_messages.append(row)

    def queue_upsert(self, tab_title: str, row: list, new_row: list | None = None) -> None:
        tab = UPSERT_TABS[tab_title]
        with self._lock:
            self._pending_upserts.setdefault(tab_title, {})[_row_key(tab, row)] = (row, new_row or row)

    def queue_client(self, chat_id: int, row: list, new_row: list | None = None) -> None:
        self.queue_upsert(CLIENTS_TAB, row, new_row)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending_messages) + sum(len(v) for v in self._pending_upserts.values())

    # --- підключення ---
    def _open(self):
        if self._spreadsheet is None:
            gc = self.client_factory(self.credentials)
            self._spreadsheet = gc.open_by_key(self.sheet_id)
        return self._spreadsheet

    def _worksheet(self, title: str, header: list[str]):
        ss = self._open()
        titles = [ws.title for ws in ss.worksheets()]
        if title in titles:
            ws = ss.worksheet(title)
        else:
            ws = ss.add_worksheet(title=title, rows=1000, cols=len(header))
        first_row = ws.row_values(1)
        # Порожній заголовок або старий коротший — дописуємо (нові стовпці додаються справа).
        if not first_row or (first_row != header and header[:len(first_row)] == first_row):
            ws.update(range_name="A1", values=[header])
        return ws

    def check_connection(self) -> str:
        """Перевіряє доступ і створює вкладки. Повертає назву таблиці."""
        self._worksheet(MESSAGES_TAB, MESSAGES_HEADER)
        self._worksheet(CLIENTS_TAB, CLIENTS_HEADER)
        self._worksheet(TASKS_TAB, TASKS_HEADER)
        self._worksheet(DONE_TASKS_TAB, TASKS_HEADER)
        return self._open().title

    # --- вкладки-списки «Партнери» і «Холодні» (переписуються цілком) ---
    def write_list(self, title: str, header: list[str], rows: list[list]) -> None:
        try:
            ss = self._open()
            titles = [ws.title for ws in ss.worksheets()]
            if title in titles:
                ws = ss.worksheet(title)
                ws.clear()
            else:
                ws = ss.add_worksheet(title=title, rows=max(100, len(rows) + 10), cols=len(header))
            ws.update(range_name="A1", values=[header] + rows, value_input_option="RAW")
        except Exception:
            self._reset()
            raise

    def write_cold_list(self, rows: list[list]) -> None:
        self.write_list(COLD_TAB, COLD_HEADER, rows)

    def write_partner_list(self, rows: list[list]) -> None:
        self.write_list(PARTNERS_TAB, PARTNERS_HEADER, rows)

    def write_task_lists(self, open_rows: list[list], done_rows: list[list]) -> None:
        """«Задачі» — лише невиконані; «Виконані задачі» — виконані, з текстом дотику й відповіддю."""
        self.write_list(TASKS_TAB, TASKS_HEADER, open_rows)
        self.write_list(DONE_TASKS_TAB, TASKS_HEADER, done_rows)

    # --- вкладка «Статистика» ---
    def write_stats(self, grid: list[list]) -> None:
        """Перезаписує вкладку «Статистика» і (один раз) створює діаграми."""
        from .stats import STATS_TAB
        try:
            ss = self._open()
            titles = [ws.title for ws in ss.worksheets()]
            if STATS_TAB in titles:
                ws = ss.worksheet(STATS_TAB)
            else:
                ws = ss.add_worksheet(title=STATS_TAB, rows=max(60, len(grid) + 5), cols=20)
            ws.update(range_name="A1", values=grid, value_input_option="RAW")
            self._ensure_charts(ss, ws)
        except Exception:
            self._reset()
            raise

    def _ensure_charts(self, ss, ws) -> None:
        from .stats import chart_requests, format_requests
        meta = ss.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId,title),charts(chartId))"})
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["sheetId"] == ws.id:
                if sheet.get("charts"):
                    return
        ss.batch_update({"requests": format_requests(ws.id) + chart_requests(ws.id)})

    def _reset(self) -> None:
        self._spreadsheet = None
        self._row_index = {}

    # --- читання позначок «Не клієнт» ---
    def read_not_client_flags(self) -> dict[int, bool]:
        try:
            ws = self._worksheet(CLIENTS_TAB, CLIENTS_HEADER)
            values = ws.get_all_values()
        except Exception:
            self._reset()
            raise
        if not values:
            return {}
        header = [h.strip() for h in values[0]]
        try:
            id_col = header.index("ID чату")
            flag_col = header.index(NOT_CLIENT_COLUMN)
        except ValueError:
            return {}
        flags: dict[int, bool] = {}
        for row in values[1:]:
            if id_col >= len(row):
                continue
            try:
                chat_id = int(str(row[id_col]).strip())
            except ValueError:
                continue
            flags[chat_id] = is_truthy(row[flag_col]) if flag_col < len(row) else False
        return flags

    # --- відправка пачки ---
    def flush(self) -> int:
        """Відправляє все з черги. Повертає кількість відправлених рядків.
        Якщо сталася помилка — невідправлені рядки повертаються в чергу, а помилка йде далі."""
        with self._lock:
            messages = self._pending_messages
            upserts = self._pending_upserts
            self._pending_messages = []
            self._pending_upserts = {}
        total = len(messages) + sum(len(v) for v in upserts.values())
        if not total:
            return 0
        try:
            if messages:
                ws = self._worksheet(MESSAGES_TAB, MESSAGES_HEADER)
                ws.append_rows(messages, value_input_option="RAW")
                messages = []
            for title in list(upserts):
                self._write_upserts(UPSERT_TABS[title], upserts[title])
                del upserts[title]
            return total
        except Exception:
            self._reset()
            with self._lock:
                self._pending_messages = messages + self._pending_messages
                for title, items in upserts.items():
                    target = self._pending_upserts.setdefault(title, {})
                    for key, rows in items.items():
                        # якщо за цей час прийшов свіжіший рядок — він важливіший
                        target.setdefault(key, rows)
            raise

    def _index(self, tab: Tab, ws) -> dict[str, int]:
        if tab.title not in self._row_index:
            values = ws.get_all_values()
            self._row_index[tab.title] = {
                _row_key(tab, row): i + 1 for i, row in enumerate(values) if i > 0 and any(row)}
        return self._row_index[tab.title]

    def _write_upserts(self, tab: Tab, items: dict[str, tuple[list, list]]) -> None:
        ws = self._worksheet(tab.title, tab.header)
        index = self._index(tab, ws)
        segments = tab.update_segments()
        updates, new_rows = [], []
        for key, (row, new_row) in items.items():
            row_no = index.get(key)
            if row_no:
                for start, end in segments:
                    if start >= len(row):
                        continue
                    updates.append({"range": f"{_col_letter(start + 1)}{row_no}:{_col_letter(end)}{row_no}",
                                    "values": [row[start:end]]})
            else:
                new_rows.append(new_row)
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
            # Після додавання перечитуємо номери рядків, щоб наступні оновлення лягли правильно.
            self._row_index.pop(tab.title, None)
