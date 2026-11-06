"""Запис у Google Таблицю.

Рядки не пишуться в таблицю миттєво: вони збираються в чергу і раз на кілька
секунд відправляються однією пачкою. Так ми не впираємося в ліміти Google,
а якщо Google тимчасово недоступний — рядки чекають і відправляються пізніше.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

MESSAGES_TAB = "Повідомлення"
CLIENTS_TAB = "Клієнти"

MESSAGES_HEADER = ["Дата", "Клієнт", "Username", "ID чату", "Хто написав", "Текст"]
CLIENTS_HEADER = ["ID чату", "Клієнт", "Username", "Тип", "Перший контакт",
                  "Останнє повідомлення", "Хто писав останнім", "Привітання надіслано", "Етап"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def default_client_factory(credentials: dict):
    import gspread
    return gspread.service_account_from_dict(credentials, scopes=SCOPES)


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetWriter:
    def __init__(self, sheet_id: str, credentials: dict,
                 client_factory: Callable[[dict], object] = default_client_factory):
        self.sheet_id = sheet_id
        self.credentials = credentials
        self.client_factory = client_factory
        self._lock = threading.Lock()
        self._pending_messages: list[list] = []
        self._pending_clients: dict[int, list] = {}
        self._spreadsheet = None
        self._client_rows: dict[str, int] | None = None  # chat_id -> номер рядка у вкладці «Клієнти»

    # --- черга ---
    def queue_message(self, row: list) -> None:
        with self._lock:
            self._pending_messages.append(row)

    def queue_client(self, chat_id: int, row: list) -> None:
        with self._lock:
            self._pending_clients[chat_id] = row

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending_messages) + len(self._pending_clients)

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
        if not first_row:
            ws.update(range_name="A1", values=[header])
        return ws

    def check_connection(self) -> str:
        """Перевіряє доступ і створює вкладки. Повертає назву таблиці."""
        self._worksheet(MESSAGES_TAB, MESSAGES_HEADER)
        self._worksheet(CLIENTS_TAB, CLIENTS_HEADER)
        return self._open().title

    # --- відправка пачки ---
    def flush(self) -> int:
        """Відправляє все з черги. Повертає кількість відправлених рядків.
        Якщо сталася помилка — рядки повертаються в чергу, а помилка йде далі."""
        with self._lock:
            messages = self._pending_messages
            clients = self._pending_clients
            self._pending_messages = []
            self._pending_clients = {}
        if not messages and not clients:
            return 0
        total = len(messages) + len(clients)
        try:
            if messages:
                ws = self._worksheet(MESSAGES_TAB, MESSAGES_HEADER)
                ws.append_rows(messages, value_input_option="RAW")
                messages = []
            if clients:
                self._write_clients(clients)
                clients = {}
            return total
        except Exception:
            self._spreadsheet = None
            self._client_rows = None
            with self._lock:
                self._pending_messages = messages + self._pending_messages
                for chat_id, row in clients.items():
                    # якщо за цей час прийшов свіжіший рядок клієнта — він важливіший
                    self._pending_clients.setdefault(chat_id, row)
            raise

    def _write_clients(self, clients: dict[int, list]) -> None:
        ws = self._worksheet(CLIENTS_TAB, CLIENTS_HEADER)
        if self._client_rows is None:
            ids = ws.col_values(1)
            self._client_rows = {str(v): i + 1 for i, v in enumerate(ids) if i > 0 and v}
        updates, new_rows, new_ids = [], [], []
        last_col = _col_letter(len(CLIENTS_HEADER))
        for chat_id, row in clients.items():
            row_no = self._client_rows.get(str(chat_id))
            if row_no:
                updates.append({"range": f"A{row_no}:{last_col}{row_no}", "values": [row]})
            else:
                new_rows.append(row)
                new_ids.append(str(chat_id))
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
            # Після додавання перечитуємо номери рядків, щоб наступні оновлення лягли правильно.
            ids = ws.col_values(1)
            self._client_rows = {str(v): i + 1 for i, v in enumerate(ids) if i > 0 and v}
