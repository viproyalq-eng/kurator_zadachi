import pytest

from bot.sheets import (CLIENTS_HEADER, CLIENTS_TAB, MESSAGES_HEADER, MESSAGES_TAB, TASKS_HEADER, TASKS_TAB,
                        SheetWriter)


class FakeWS:
    """Як справжній gspread.Worksheet: title, id і row_count лише для читання."""
    _next_id = 1

    def __init__(self, title):
        self._id = FakeWS._next_id
        FakeWS._next_id += 1
        self._title = title
        self._row_count = 1000
        self.rows = []
        self.fail = False

    @property
    def title(self):
        return self._title

    @property
    def id(self):
        return self._id

    @property
    def row_count(self):
        return self._row_count

    def add_rows(self, n):
        self._row_count += n

    def row_values(self, n):
        return self.rows[n - 1] if len(self.rows) >= n else []

    def update(self, range_name, values, value_input_option=None):
        assert range_name == "A1"
        for i, row in enumerate(values):
            if i < len(self.rows):
                self.rows[i] = list(row)
            else:
                self.rows.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        if self.fail:
            raise RuntimeError("quota")
        self.rows.extend(list(r) for r in rows)

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def batch_update(self, updates, value_input_option=None):
        import re
        for u in updates:
            col, row_no = re.match(r"([A-Z]+)(\d+):", u["range"]).groups()
            start = 0
            for ch in col:
                start = start * 26 + (ord(ch) - 64)
            start -= 1
            row_no = int(row_no)
            new = list(u["values"][0])
            old = self.rows[row_no - 1]
            old += [""] * (start + len(new) - len(old))
            # як у справжній таблиці: змінюються лише стовпці з діапазону, решта лишається
            old[start:start + len(new)] = new


class FakeSS:
    title = "Віктор — статистика"

    def __init__(self):
        self.tabs = {}

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]

    def worksheet(self, title):
        for ws in self.tabs.values():
            if ws.title == title:
                return ws
        raise KeyError(title)


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


# --- зрозумілі пояснення помилок ---

class FakeResponse:
    def __init__(self, code, message):
        self.status_code = code
        self._msg = message
        self.text = message

    def json(self):
        return {"error": {"code": self.status_code, "message": self._msg, "status": "X"}}


def _wrapped(code, message, outer):
    """Як у gspread: APIError, загорнута в PermissionError/SpreadsheetNotFound."""
    from gspread.exceptions import APIError
    try:
        try:
            raise APIError(FakeResponse(code, message))
        except APIError as inner:
            raise outer from inner
    except Exception as e:  # noqa: BLE001
        return e


def test_explain_no_access():
    from bot.sheets import explain_error
    e = _wrapped(403, "The caller does not have permission", PermissionError())
    text = explain_error(e, "bot@proj.iam.gserviceaccount.com")
    assert "Поділитися" in text and "bot@proj.iam.gserviceaccount.com" in text


def test_explain_api_disabled():
    from bot.sheets import explain_error
    e = _wrapped(403, "Google Sheets API has not been used in project 123 before or it is disabled.",
                 PermissionError())
    text = explain_error(e, "x@y")
    assert "Google Sheets API" in text and "Enable" in text


def test_explain_not_found():
    from gspread.exceptions import SpreadsheetNotFound
    from bot.sheets import explain_error
    assert "GOOGLE_SHEET_ID" in explain_error(SpreadsheetNotFound(), None)


def test_explain_unknown():
    from bot.sheets import explain_error
    assert explain_error(RuntimeError("boom"), None) == "RuntimeError: boom"


# --- вкладка «Статистика» і діаграми ---

def _chart_api(ss):
    """Імітація Google API для діаграм: пам'ятає назви доданих діаграм по вкладках."""
    batches, charts = [], {}

    def fetch_sheet_metadata(params=None):
        return {"sheets": [{"properties": {"sheetId": ws.id, "title": ws.title},
                            "charts": [{"chartId": i, "spec": {"title": t}} for i, t in enumerate(charts.get(ws.id, []))]}
                           for ws in ss.tabs.values()]}

    def batch_update(body):
        batches.append(body)
        for r in body["requests"]:
            if "updateSheetProperties" in r:          # як Google: перейменовує вкладку
                props = r["updateSheetProperties"]["properties"]
                for ws in ss.tabs.values():
                    if ws.id == props["sheetId"]:
                        ws._title = props["title"]
            if "addChart" in r:
                sid = r["addChart"]["chart"]["position"]["overlayPosition"]["anchorCell"]["sheetId"]
                charts.setdefault(sid, []).append(r["addChart"]["chart"]["spec"]["title"])

    ss.fetch_sheet_metadata = fetch_sheet_metadata
    ss.batch_update = batch_update
    return batches, charts


def test_write_stats_creates_charts_once(writer):
    w, ss = writer
    batches, charts = _chart_api(ss)
    grid = [["a", 1], ["b", 2]]
    w.write_stats(grid)
    w.write_stats(grid)
    assert ss.tabs["Діаграми ефективності"].rows == grid
    assert len(batches) == 1
    assert sum("addChart" in r for r in batches[0]["requests"]) == 9


def test_old_statistics_tab_is_renamed_and_only_new_charts_added(writer):
    from bot.stats import chart_specs
    w, ss = writer
    batches, charts = _chart_api(ss)
    old = ss.add_worksheet("Статистика", 60, 20)
    old._row_count = 60
    charts[old.id] = [t for t, _, _ in chart_specs(old.id)[:3]]   # три старі діаграми вже є
    w.write_stats([["x"]] * 120)
    renames = [r for b in batches for r in b["requests"] if "updateSheetProperties" in r]
    assert renames and renames[0]["updateSheetProperties"]["properties"]["title"] == "Діаграми ефективності"
    assert old.title == "Діаграми ефективності" and old.rows[0] == ["x"]
    assert old.row_count >= 125                        # рядків вистачає для трендів
    assert len(charts[old.id]) == 9                    # додано лише 6 нових, без дублів


def test_partner_columns_updated_but_not_client_kept(writer):
    w, ss = writer
    row = ["100", "Іван", "", "новий", "", "", "клієнт", "", "", "", "Інвестор", ""]
    w.queue_client(100, row)
    w.flush()
    ss.tabs[CLIENTS_TAB].rows[1][9] = "так"
    w.queue_client(100, row[:10] + ["Партнер", "06.11.2026 10:00"])
    w.flush()
    assert ss.tabs[CLIENTS_TAB].rows[1][9:] == ["так", "Партнер", "06.11.2026 10:00"]



def test_list_tab_is_rewritten(writer):
    w, ss = writer

    def clear():
        ss.tabs["Партнери"].rows = []

    w.write_partner_list([["Іван", "@ivan"], ["Олена", ""]])
    ss.tabs["Партнери"].clear = clear
    w.write_partner_list([["Олена", ""]])
    rows = ss.tabs["Партнери"].rows
    assert rows[0][0] == "Клієнт" and rows[1] == ["Олена", ""] and len(rows) == 2



def test_task_lists_move_between_tabs(writer):
    w, ss = writer
    w.write_task_lists([["07.11.2026", "Іван", "", "1"], ["07.11.2026", "Оля", "", "2"]], [])
    assert len(ss.tabs["Задачі"].rows) == 3 and ss.tabs["Виконані задачі"].rows[0][0] == "Дата задачі"
    for title in ("Задачі", "Виконані задачі"):
        ss.tabs[title].clear = (lambda t=title: setattr(ss.tabs[t], "rows", []))
    w.write_task_lists([["07.11.2026", "Оля", "", "2"]], [["07.11.2026", "Іван", "", "1", "", "", "07.11.2026 10:00"]])
    assert [r[1] for r in ss.tabs["Задачі"].rows[1:]] == ["Оля"]
    assert [r[1] for r in ss.tabs["Виконані задачі"].rows[1:]] == ["Іван"]



def test_write_dashboard(writer):
    w, ss = writer
    batches = []
    ss.batch_update = lambda body: batches.append(body)
    rows = [["📈 Ефективність куратора — Віктор", "", ""], ["", "", ""], ["⏱ 1. Швидкість", "", ""]]
    w.write_dashboard("Ефективність куратора", rows, [(0, "title"), (2, "section")])
    assert ss.tabs["Ефективність куратора"].rows[0][0].startswith("📈")
    reqs = batches[0]["requests"]
    colored = [r for r in reqs if "backgroundColor" in r.get("repeatCell", {}).get("cell", {})
               .get("userEnteredFormat", {})]
    assert [r["repeatCell"]["range"]["startRowIndex"] for r in colored] == [0, 2]
