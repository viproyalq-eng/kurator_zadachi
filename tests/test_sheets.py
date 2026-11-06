import pytest

from bot.sheets import (CLIENTS_HEADER, CLIENTS_TAB, MESSAGES_HEADER, MESSAGES_TAB, TASKS_HEADER, TASKS_TAB,
                        SheetWriter)


class FakeWS:
    def __init__(self, title):
        self.title = title
        self.rows = []
        self.fail = False

    def row_values(self, n):
        return self.rows[n - 1] if len(self.rows) >= n else []

    def update(self, range_name, values):
        assert range_name == "A1"
        if self.rows:
            self.rows[0] = values[0]
        else:
            self.rows.append(values[0])

    def append_rows(self, rows, value_input_option=None):
        if self.fail:
            raise RuntimeError("quota")
        self.rows.extend(list(r) for r in rows)

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def batch_update(self, updates, value_input_option=None):
        for u in updates:
            row_no = int(u["range"].split(":")[0][1:])
            new = list(u["values"][0])
            old = self.rows[row_no - 1]
            # як у справжній таблиці: змінюються лише стовпці з діапазону, решта лишається
            self.rows[row_no - 1] = new + old[len(new):]


class FakeSS:
    title = "Віктор — статистика"

    def __init__(self):
        self.tabs = {}

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, title):
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]


@pytest.fixture
def writer():
    ss = FakeSS()

    class GC:
        def open_by_key(self, key):
            assert key == "sid"
            return ss

    w = SheetWriter("sid", {"client_email": "x"}, client_factory=lambda creds: GC())
    return w, ss


def test_creates_tabs_with_headers(writer):
    w, ss = writer
    assert w.check_connection() == "Віктор — статистика"
    assert ss.tabs[MESSAGES_TAB].rows[0] == MESSAGES_HEADER
    assert ss.tabs[CLIENTS_TAB].rows[0] == CLIENTS_HEADER
    assert ss.tabs[TASKS_TAB].rows[0] == TASKS_HEADER


def test_flush_messages_and_upsert_clients(writer):
    w, ss = writer
    w.queue_message(["05.11.2026 10:00", "Іван", "@ivan", "100", "клієнт", "бонус"])
    w.queue_client(100, ["100", "Іван", "@ivan", "новий", "", "", "клієнт", "", ""])
    w.queue_client(200, ["200", "Оля", "", "старий", "", "", "Віктор", "", ""])
    assert w.flush() == 3
    clients = ss.tabs[CLIENTS_TAB].rows
    assert len(clients) == 3
    # оновлення існуючого клієнта — той самий рядок, не новий
    w.queue_client(100, ["100", "Іван", "@ivan", "новий", "", "", "бот", "так", ""])
    w.flush()
    clients = ss.tabs[CLIENTS_TAB].rows
    assert len(clients) == 3
    assert clients[1][6] == "бот"
    assert len(ss.tabs[MESSAGES_TAB].rows) == 2
    assert w.pending_count() == 0


def test_failure_keeps_rows_in_queue(writer):
    w, ss = writer
    w.check_connection()
    ss.tabs[MESSAGES_TAB].fail = True
    w.queue_message(["a"])
    w.queue_client(1, ["1"])
    with pytest.raises(RuntimeError):
        w.flush()
    assert w.pending_count() == 2
    ss.tabs[MESSAGES_TAB].fail = False
    assert w.flush() == 2
    assert w.pending_count() == 0


def test_empty_flush(writer):
    w, _ = writer
    assert w.flush() == 0


def test_old_header_is_extended(writer):
    w, ss = writer
    ws = ss.add_worksheet(CLIENTS_TAB, 10, 9)
    ws.rows.append(CLIENTS_HEADER[:9])
    w.check_connection()
    assert ss.tabs[CLIENTS_TAB].rows[0] == CLIENTS_HEADER


def test_not_client_column_is_never_overwritten(writer):
    w, ss = writer
    w.queue_client(100, ["100", "Іван", "", "старий", "", "", "клієнт", "", ""],
                   ["100", "Іван", "", "старий", "", "", "клієнт", "", "", ""])
    w.flush()
    ss.tabs[CLIENTS_TAB].rows[1][9] = "так"   # Віктор поставив позначку
    w.queue_client(100, ["100", "Іван", "", "старий", "", "", "Віктор", "", ""])
    w.flush()
    assert ss.tabs[CLIENTS_TAB].rows[1][6] == "Віктор"
    assert ss.tabs[CLIENTS_TAB].rows[1][9] == "так"
    assert w.read_not_client_flags() == {100: True}


@pytest.mark.parametrize("value,expected", [("так", True), ("TRUE", True), ("✅", True), ("x", True),
                                            ("", False), ("FALSE", False), ("ні", False)])
def test_flag_values(writer, value, expected):
    w, ss = writer
    w.check_connection()
    ss.tabs[CLIENTS_TAB].rows.append(["200", "Оля", "", "", "", "", "", "", "", value])
    assert w.read_not_client_flags() == {200: expected}


def test_tasks_upsert_by_date_and_chat(writer):
    w, ss = writer
    w.queue_upsert(TASKS_TAB, ["06.11.2026", "Іван", "", "100", "", "40", ""])
    w.queue_upsert(TASKS_TAB, ["06.11.2026", "Оля", "", "200", "", "30", ""])
    w.flush()
    w.queue_upsert(TASKS_TAB, ["06.11.2026", "Іван", "", "100", "", "40", "06.11.2026 10:00"])
    w.queue_upsert(TASKS_TAB, ["07.11.2026", "Оля", "", "200", "", "31", ""])
    w.flush()
    rows = ss.tabs[TASKS_TAB].rows
    assert len(rows) == 4
    assert rows[1][6] == "06.11.2026 10:00"
