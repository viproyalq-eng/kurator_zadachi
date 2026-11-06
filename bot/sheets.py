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

MESSAGES_HEADER = ["Дата", "Клієнт", "Username", "ID чату", "Хто написав", "Текст"]
CLIENTS_HEADER = ["ID чату", "Клієнт", "Username", "Тип", "Перший контакт",
                  "Останнє повідомлення", "Хто писав останнім", "Привітання надіслано", "Етап",
                  "Не клієнт"]
CLIENTS_BOT_COLUMNS = 9  # A–I бот оновлює; «Не клієнт» (J) — лише при першому додаванні рядка
NOT_CLIENT_COLUMN = "Не клієнт"
TASKS_HEADER = ["Дата задачі", "Клієнт", "Username", "ID чату", "Останній контакт",
                "Днів без контакту", "Виконано"]

TRUTHY = {"так", "да", "yes", "y", "1", "x", "х", "+", "true", "✓", "✔", "✅", "v", "не клієнт"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass(frozen=True)
class Tab:
    title: str
    header: list
    key_cols: tuple   # стовпці, з яких складається «ключ» рядка
    update_width: int  # скільки перших стовпців бот перезаписує при оновленні


CLIENTS = Tab(CLIENTS_TAB, CLIENTS_HEADER, (0,), CLIENTS_BOT_COLUMNS)
TASKS = Tab(TASKS_TAB, TASKS_HEADER, (0, 3), len(TASKS_HEADER))
UPSERT_TABS = {CLIENTS.title: CLIENTS, TASKS.title: TASKS}


def is_truthy(value) -> bool:
    return str(value).strip().lower() in TRUTHY


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
        return self._open().title

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
        last_col = _col_letter(tab.update_width)
        updates, new_rows = [], []
        for key, (row, new_row) in items.items():
            row_no = index.get(key)
            if row_no:
                updates.append({"range": f"A{row_no}:{last_col}{row_no}", "values": [row[:tab.update_width]]})
            else:
                new_rows.append(new_row)
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
            # Після додавання перечитуємо номери рядків, щоб наступні оновлення лягли правильно.
            self._row_index.pop(tab.title, None)
