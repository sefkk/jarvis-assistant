"""google_sheets tools against a fake googleapiclient (no network, no Keychain)."""

from __future__ import annotations

from datetime import date

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.security.taint import TaintState
from jarvis.tools import google_sheets as gs
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.utils import google_auth, secrets
from jarvis.utils import sheet_table as st
from tests.conftest import RecordingSink
from tests.unit.test_gmail import FakeService, http_error

ID_A = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdA"
ID_B = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdB"
ID_C = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdC"
ALIAS_ID = "1AliasAliasAliasAliasAliasAliasAlias0000"
URL = f"https://docs.google.com/spreadsheets/d/{ID_C}/edit#gid=0"


def drive_file(file_id: str, name: str, modified: str = "2026-10-10T09:00:00.000Z") -> dict:
    return {"id": file_id, "name": name, "modifiedTime": modified}


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()
    fake.apis = []
    fake.required = []
    monkeypatch.setattr(google_auth, "get_credentials", lambda required=None: fake.required.append(required) or "creds")

    def build(api, version, credentials):
        fake.apis.append((api, version, credentials))
        return fake

    monkeypatch.setattr(google_auth, "_discovery_build", build)
    fake.responses["spreadsheets.get"] = {
        "properties": {"title": "Antrenman"},
        "sheets": [
            {"properties": {"title": "Log", "sheetId": 0, "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
            {"properties": {"title": "My Tab", "sheetId": 1, "gridProperties": {"rowCount": 50, "columnCount": 5}}},
        ],
    }
    fake.responses["files.list"] = {"files": [drive_file(ID_A, "Antrenman")]}
    return fake


def make_ctx(config, paths, *, sink=None, user_text=None, tainted=False, runtime=True) -> ToolContext:
    ctx = ToolContext.build(config, paths)
    config.sheets.aliases = {"antrenman": ALIAS_ID}
    if runtime:
        taint = TaintState()
        if tainted:
            taint.mark("gmail")
        ctx.runtime = Runtime(
            client=None, db=None, usage_tracker=None, registry=None, budget=None, sink=sink, user_text=user_text, taint=taint
        )
    return ctx


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    return make_ctx(config, paths)


def run(tool_cls, ctx, **args):
    tool = tool_cls(ctx)
    return tool.run(tool.parse_args(args))


def calls(service, path):
    return [kw for p, kw in service.calls if p == path]


# --- metadata ---------------------------------------------------------------------------------


def test_tool_metadata_reads_are_safe_writes_confirm_no_delete():
    reads = (gs.FindSpreadsheet, gs.ListSheets, gs.ReadRange, gs.QuerySheet)
    writes = (gs.AppendRows, gs.UpdateRange, gs.CreateSpreadsheet)
    assert [t.name for t in reads + writes] == [
        "find_spreadsheet",
        "list_sheets",
        "read_range",
        "query_sheet",
        "append_rows",
        "update_range",
        "create_spreadsheet",
    ]
    for t in reads:
        assert t.risk == RiskLevel.SAFE and t.delegate_ok is True
    for t in writes:
        assert t.risk == RiskLevel.CONFIRM and t.delegate_ok is False and not t.taints
    assert gs.ReadRange.taints and gs.ReadRange.taint_source == "sheets"
    assert gs.QuerySheet.taints and gs.QuerySheet.taint_source == "sheets"
    assert not gs.FindSpreadsheet.taints and not gs.ListSheets.taints
    import inspect

    source = inspect.getsource(gs)
    assert ".delete(" not in source and "batchUpdate" not in source and "files().delete" not in source


def test_services_ask_for_the_sheets_scopes_only(service, ctx):
    run(gs.ListSheets, ctx, spreadsheet=ID_A)
    run(gs.FindSpreadsheet, ctx, name="x")
    assert service.required and all(r == google_auth.SHEETS_SCOPES for r in service.required)
    assert {(a, v) for a, v, _ in service.apis} == {("sheets", "v4"), ("drive", "v3")}


# --- find_spreadsheet ----------------------------------------------------------------------------


def test_find_spreadsheet_lists_exact_matches_first(service, ctx):
    service.responses["files.list"] = {
        "files": [
            drive_file(ID_A, "Bütçe 2025", "2026-10-12T10:00:00Z"),
            drive_file(ID_B, "Bütçe", "2026-01-01T10:00:00Z"),
            drive_file(ID_C, "bütçe taslak"),
        ]
    }
    out = run(gs.FindSpreadsheet, ctx, name="Bütçe").content
    lines = out.splitlines()
    assert lines[0].startswith("3 spreadsheet(s) matching 'Bütçe'") and "ask the user" in lines[0]
    assert lines[1].startswith("Bütçe | id " + ID_B) and "modified 2026-01-01" in lines[1]
    assert lines[2].startswith("Bütçe 2025 | id " + ID_A)
    assert lines[3].startswith("bütçe taslak | id " + ID_C)
    kw = calls(service, "files.list")[0]
    assert kw["q"] == "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false and name contains 'Bütçe'"
    assert kw["fields"] == "files(id,name,modifiedTime)" and kw["pageSize"] == 50 and kw["orderBy"] == "modifiedTime desc"


def test_find_spreadsheet_without_matches_and_empty_name(service, ctx):
    service.responses["files.list"] = {"files": []}
    assert run(gs.FindSpreadsheet, ctx, name="nothing").content == "No spreadsheet title contains 'nothing'."
    with pytest.raises(ToolValidationError, match="name is empty"):
        run(gs.FindSpreadsheet, ctx, name="   ")


# --- resolution -----------------------------------------------------------------------------------


def test_url_id_and_alias_resolve_without_a_drive_search(service, ctx):
    assert f"({ID_C})" in run(gs.ListSheets, ctx, spreadsheet=URL).content
    assert f"({ID_A})" in run(gs.ListSheets, ctx, spreadsheet=ID_A).content
    assert f"({ALIAS_ID})" in run(gs.ListSheets, ctx, spreadsheet="ANTRENMAN").content  # alias, case-insensitive
    assert calls(service, "files.list") == []
    assert [kw["spreadsheetId"] for kw in calls(service, "spreadsheets.get")] == [ID_C, ID_C, ID_A, ID_A, ALIAS_ID, ALIAS_ID]


def test_name_resolves_through_drive_exact_match_wins_over_contains(service, ctx):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Koşu 2024"), drive_file(ID_B, "koşu")]}
    out = run(gs.ListSheets, ctx, spreadsheet="Koşu").content
    assert f"'koşu' ({ID_B})" in out  # the title comes from Drive, no title lookup needed
    assert calls(service, "files.list")[0]["q"].endswith("name contains 'Koşu'")
    assert calls(service, "spreadsheets.get")[0]["fields"] == "properties.title,sheets.properties"  # list_sheets only


def test_single_contains_match_is_used(service, ctx):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Antrenman 2024")]}
    assert f"'Antrenman 2024' ({ID_A})" in run(gs.ListSheets, ctx, spreadsheet="antren").content


def test_zero_matches_is_an_execution_error(service, ctx):
    service.responses["files.list"] = {"files": []}
    with pytest.raises(ToolExecutionError, match="no spreadsheet named 'Yok'"):
        run(gs.ListSheets, ctx, spreadsheet="Yok")
    with pytest.raises(ToolValidationError, match="spreadsheet must be"):
        run(gs.ListSheets, ctx, spreadsheet="  ")


def test_ambiguous_name_asks_the_user_through_the_sink(service, config, paths):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe", "2026-10-12T10:00:00Z"), drive_file(ID_B, "Bütçe")]}
    sink = RecordingSink(choice_answer=ID_B)
    ctx = make_ctx(config, paths, sink=sink)
    out = run(gs.ListSheets, ctx, spreadsheet="Bütçe").content
    assert f"({ID_B})" in out
    question, options, default = sink.choices[0]
    assert "Bütçe" in question and default == "none"
    assert [k for k, _ in options] == [ID_A, ID_B, "none"]
    assert options[0][1].startswith("Bütçe (değiştirilme: 2026-10-12") and options[-1] == ("none", "hiçbiri")  # Turkish test config


def test_ambiguous_name_with_none_chosen_tells_the_model_to_ask(service, config, paths):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe"), drive_file(ID_B, "Bütçe")]}
    ctx = make_ctx(config, paths, sink=RecordingSink(choice_answer="none"))
    with pytest.raises(ToolValidationError, match="ambiguous") as info:
        run(gs.ListSheets, ctx, spreadsheet="Bütçe")
    text = str(info.value)
    assert f"Bütçe (id {ID_A})" in text and f"Bütçe (id {ID_B})" in text and "Ask the user" in text
    assert calls(service, "spreadsheets.get") == []  # nothing was guessed


def test_ambiguous_name_without_a_sink_lists_the_candidates(service, config, paths):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe"), drive_file(ID_B, "Bütçe")]}
    for ctx in (make_ctx(config, paths, runtime=False), make_ctx(config, paths, sink=None)):
        with pytest.raises(ToolValidationError, match="ambiguous.*2 matches") as info:
            run(gs.ListSheets, ctx, spreadsheet="Bütçe")
        assert ID_A in str(info.value) and ID_B in str(info.value)


def test_sink_failure_during_the_choice_counts_as_none(service, config, paths):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe"), drive_file(ID_B, "Bütçe")]}

    class BrokenSink(RecordingSink):
        def ask_choice(self, question, options, default):
            raise RuntimeError("terminal gone")

    ctx = make_ctx(config, paths, sink=BrokenSink())
    with pytest.raises(ToolValidationError, match="ambiguous"):
        run(gs.ListSheets, ctx, spreadsheet="Bütçe")


# --- list_sheets ------------------------------------------------------------------------------------


def test_list_sheets_shows_tabs_and_sizes(service, ctx):
    out = run(gs.ListSheets, ctx, spreadsheet=ID_A).content
    assert out.splitlines() == [
        f"Spreadsheet 'Antrenman' ({ID_A}): 2 tab(s) (rows x columns):",
        "Log | 1000 x 26",
        "My Tab | 50 x 5",
    ]
    assert calls(service, "spreadsheets.get")[-1]["fields"] == "properties.title,sheets.properties"


def test_api_errors_become_short_tool_errors(service, ctx):
    service.responses["spreadsheets.get"] = http_error(404, "Requested entity was not found.")
    with pytest.raises(ToolExecutionError, match="opening the spreadsheet: not found"):
        run(gs.ListSheets, ctx, spreadsheet=ID_A)


# --- read_range and query_sheet -------------------------------------------------------------------------

TODAY = date(2026, 10, 17)


def serial(d: date) -> int:
    return (d - st.SHEETS_EPOCH).days


def api_cell(text, value=None, kind=None) -> dict:
    cell: dict = {}
    if text is not None:
        cell["formattedValue"] = text
    if isinstance(value, bool):
        cell["effectiveValue"] = {"boolValue": value}
    elif isinstance(value, (int, float)):
        cell["effectiveValue"] = {"numberValue": value}
    elif isinstance(value, str):
        cell["effectiveValue"] = {"stringValue": value}
    if kind:
        cell["effectiveFormat"] = {"numberFormat": {"type": kind}}
    return cell


def d(text, day: date) -> dict:
    return api_cell(text, serial(day), "DATE")


def n(text, value=None) -> dict:
    return api_cell(text, float(text) if value is None else value)


def t(text) -> dict:
    return api_cell(text, text)


def grid_response(rows: list[list[dict]], *, title="Sayfa1", start_row=None, start_col=None, meta_title="Antrenman"):
    """A callable for `spreadsheets.get`: grid data when read with includeGridData, metadata otherwise."""
    data: dict = {"rowData": [{"values": r} if r else {} for r in rows]}
    if start_row is not None:
        data["startRow"] = start_row
    if start_col is not None:
        data["startColumn"] = start_col

    def respond(kw):
        if kw.get("includeGridData"):
            return {"sheets": [{"properties": {"title": title}, "data": [data]}]}
        return {
            "properties": {"title": meta_title},
            "sheets": [{"properties": {"title": title, "gridProperties": {"rowCount": 1000, "columnCount": 26}}}],
        }

    return respond


# Synthetic cash-flow tab shaped like the real incident: row 1 empty, header on row 2 with an empty first
# cell, ragged rows, a balance column right before the income column.
BUDGET_ROWS = [
    [],
    [{}, t("Bakiye"), t("Gelir"), t("Nereden"), t("Gider"), t("Nereye")],
    [d("16 Ekim", date(2026, 10, 16)), n("0"), n("67.500", 67500), t("fon"), n("67500"), t("ing kk")],
    [d("17 Ekim", date(2026, 10, 17)), n("0")],
    [d("18 Ekim", date(2026, 10, 18)), n("0"), n("30000"), t("fon")],
    [d("19 Ekim", date(2026, 10, 19)), n("30000"), n("20000"), t("Fon")],
    [d("20 Ekim", date(2026, 10, 20)), n("50000")],
    [d("21 Ekim", date(2026, 10, 21)), n("50000"), {}, {}, n("12000"), t("HARCAMA DÜZELTME")],
    [d("26 Ekim", date(2026, 10, 26)), n("24000"), n("75000"), t("fon")],
]


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(st, "today_local", lambda: TODAY)
    return TODAY


def test_read_range_returns_padded_labeled_rows(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    out = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="Sayfa1").content
    lines = out.splitlines()
    assert lines[0] == f"Spreadsheet 'Antrenman' ({ID_A}), range Sayfa1: 7 data row(s) (sheet rows 3-9, columns A-F)"
    assert lines[1].startswith(
        "Columns: A [date, no header] | Bakiye [number] | Gelir [number] | Nereden [text] | Gider [number] | Nereye [text]"
    )
    assert "header on sheet row 2" in lines[1]
    assert lines[2] == 'Row 3: A=2026-10-16 ("16 Ekim") | Bakiye=0 | Gelir=67500 ("67.500") | Nereden="fon" | Gider=67500 | Nereye="ing kk"'
    assert lines[3] == 'Row 4: A=2026-10-17 ("17 Ekim") | Bakiye=0 | Gelir=(empty) | Nereden=(empty) | Gider=(empty) | Nereye=(empty)'
    assert lines[5] == 'Row 6: A=2026-10-19 ("19 Ekim") | Bakiye=30000 | Gelir=20000 | Nereden="Fon" | Gider=(empty) | Nereye=(empty)'
    assert (
        lines[7]
        == 'Row 8: A=2026-10-21 ("21 Ekim") | Bakiye=50000 | Gelir=(empty) | Nereden=(empty) | Gider=12000 | Nereye="HARCAMA DÜZELTME"'
    )
    assert len(lines) == 2 + 7
    kw = [kw for p, kw in service.calls if p == "spreadsheets.get" and kw.get("includeGridData")][0]
    assert kw["spreadsheetId"] == ID_A and kw["ranges"] == ["Sayfa1"] and kw["fields"] == gs._GRID_FIELDS
    assert [p for p, _ in service.calls if p == "spreadsheets.values.get"] == []


def test_read_range_empty_and_whole_tab(service, ctx):
    service.responses["spreadsheets.get"] = grid_response([])
    out = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="My Tab").content
    assert out.endswith("range My Tab: empty.")
    assert [kw["ranges"] for p, kw in service.calls if p == "spreadsheets.get" and kw.get("includeGridData")] == [["My Tab"]]


def test_read_range_sub_range_keeps_sheet_row_numbers_and_letters(service, ctx, today):
    rows = [[t("Tutar"), t("Not")], [n("1.5", 1.5), t("x")], [n("2")]]
    service.responses["spreadsheets.get"] = grid_response(rows, start_row=9, start_col=2)
    lines = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="Sayfa1!C10:D12").content.splitlines()
    assert lines[0].endswith("2 data row(s) (sheet rows 11-12, columns C-D)")
    assert lines[2] == 'Row 11: Tutar=1.5 | Not="x"' and lines[3] == "Row 12: Tutar=2 | Not=(empty)"


def test_read_range_truncates_by_rows_and_points_to_query_sheet(service, config, paths, today):
    config.context.tool_result_max_chars = 600
    ctx = make_ctx(config, paths)
    rows = [[t("Ad"), t("Not")]] + [[t(f"row{i:03d}"), t("x" * 40)] for i in range(100)]
    service.responses["spreadsheets.get"] = grid_response(rows)
    out = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="Log").content
    lines = out.splitlines()
    assert lines[0].endswith("100 data row(s) (sheet rows 2-101, columns A-B)")
    shown = lines[2:-1]
    assert all(l.startswith("Row ") for l in shown) and 0 < len(shown) < 100
    assert lines[-1] == f"[{100 - len(shown)} more row(s) not shown; use query_sheet to filter, sort or sum, or read a smaller range]"
    assert len(out) <= 600 + len(lines[-1]) + 1  # only whole rows are kept
    assert 'Row 2: Ad="row000"' in out and shown[-1].startswith(f'Row {len(shown) + 1}: Ad="row{len(shown) - 1:03d}"')


def test_read_range_strips_invisible_characters_from_cells(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response([[t("Not")], [t("a\u200bb\tc\nd")]])
    out = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="Log!A1:A2").content
    assert out.splitlines()[-1] == 'Row 2: Not="ab c d"'


def test_read_range_without_a_header_row_uses_letters(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response([[n("1"), n("2")], [n("3")]])
    lines = run(gs.ReadRange, ctx, spreadsheet=ID_A, range="Log").content.splitlines()
    assert lines[1].startswith("Columns: A [number, no header] | B [number, no header] (no header row detected")
    assert lines[2:] == ["Row 1: A=1 | B=2", "Row 2: A=3 | B=(empty)"]


def test_query_sheet_metadata():
    assert gs.QuerySheet.risk == RiskLevel.SAFE and gs.QuerySheet.taints and gs.QuerySheet.taint_source == "sheets"
    assert gs.QuerySheet.delegate_ok is True
    schema = gs.QuerySheet().api_schema()["input_schema"]
    assert set(schema["properties"]) == {
        "spreadsheet",
        "sheet",
        "filters",
        "sort_by",
        "descending",
        "aggregate",
        "aggregate_column",
        "limit",
    }
    assert schema["required"] == ["spreadsheet", "sheet"]


def test_query_sheet_next_fund_income_after_today(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    out = run(
        gs.QuerySheet,
        ctx,
        spreadsheet=ID_A,
        sheet="Sayfa1",
        filters=[{"column": "Nereden", "op": "contains", "value": "fon"}, {"column": "A", "op": "gte", "value": "today"}],
        sort_by="A",
        aggregate="sum",
        aggregate_column="Gelir",
    ).content
    lines = out.splitlines()
    assert lines[0] == f"Spreadsheet 'Antrenman' ({ID_A}), range Sayfa1: 7 data row(s) (sheet rows 3-9, columns A-F); today is 2026-10-17"
    assert lines[1].startswith("Columns: A [date, no header] | Bakiye [number] | Gelir [number]")
    assert lines[2] == 'Filters: Nereden contains "fon"; A gte 2026-10-17 (today = 2026-10-17)'
    assert lines[3] == "Sort: A ascending"
    assert lines[4] == "Matched 3 row(s):"
    assert lines[5] == "Result: sum of Gelir over 3 matched row(s) = 125000 (3 numeric cell(s), 0 empty or non-numeric skipped)"
    assert lines[6] == 'Row 5: A=2026-10-18 ("18 Ekim") | Bakiye=0 | Gelir=30000 | Nereden="fon" | Gider=(empty) | Nereye=(empty)'
    assert lines[7] == 'Row 6: A=2026-10-19 ("19 Ekim") | Bakiye=30000 | Gelir=20000 | Nereden="Fon" | Gider=(empty) | Nereye=(empty)'
    assert lines[8] == 'Row 9: A=2026-10-26 ("26 Ekim") | Bakiye=24000 | Gelir=75000 | Nereden="fon" | Gider=(empty) | Nereye=(empty)'
    assert len(lines) == 9


def test_query_sheet_sum_of_two_days_count_and_limit(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    out = run(
        gs.QuerySheet,
        ctx,
        spreadsheet=ID_A,
        sheet="Sayfa1",
        filters=[
            {"column": "nereden", "op": "contains", "value": "FON"},
            {"column": "A", "op": "gte", "value": "18 Ekim"},
            {"column": "A", "op": "lte", "value": "2026-10-19"},
        ],
        aggregate="sum",
        aggregate_column="gelir",
    ).content
    assert "Result: sum of Gelir over 2 matched row(s) = 50000 " in out and "Row 5:" in out and "Row 6:" in out
    out = run(
        gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", filters=[{"column": "Gelir", "op": "not_empty"}], aggregate="count", limit=2
    ).content
    assert "Matched 4 row(s) (listing the first 2; raise limit or narrow the filters for the rest):" in out
    assert "Result: count of matched rows = 4" in out and out.count("\nRow ") == 2


def test_query_sheet_no_match_and_empty_range(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    out = run(
        gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", filters=[{"column": "Nereden", "op": "contains", "value": "kira"}]
    ).content
    assert out.splitlines()[-1] == "Matched 0 row(s)."
    service.responses["spreadsheets.get"] = grid_response([])
    assert run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1").content.endswith("range Sayfa1: empty.")


def test_query_sheet_unknown_column_is_a_result_not_an_exception(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    tool = gs.QuerySheet(ctx)
    res = tool.run(tool.parse_args({"spreadsheet": ID_A, "sheet": "Sayfa1", "filters": [{"column": "Tutar", "op": "gt", "value": "1"}]}))
    assert (
        res.is_error
        and res.content.startswith("query_sheet: no column named 'Tutar'")
        and "Columns of Sayfa1: A [date, no header] | Bakiye [number]" in res.content
    )


def test_query_sheet_validation(service, ctx, today):
    service.responses["spreadsheets.get"] = grid_response(BUDGET_ROWS)
    with pytest.raises(ToolValidationError, match="invalid arguments"):
        run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", filters=[{"column": "A", "op": "like", "value": "x"}])
    with pytest.raises(ToolValidationError, match="invalid arguments"):
        run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", aggregate="median", aggregate_column="Gelir")
    with pytest.raises(ToolValidationError, match="needs a column"):
        run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", aggregate="sum")
    with pytest.raises(ToolValidationError, match="needs a value"):
        run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1", filters=[{"column": "A", "op": "gt"}])
    with pytest.raises(ToolValidationError, match="not A1 notation"):
        run(gs.QuerySheet, ctx, spreadsheet=ID_A, sheet="Sayfa1; drop")


# --- append_rows --------------------------------------------------------------------------------------


def test_append_rows_request_body_and_result(service, ctx):
    service.responses["spreadsheets.values.append"] = {"updates": {"updatedRange": "Log!A5:C6"}}
    out = run(
        gs.AppendRows, ctx, spreadsheet="antrenman", sheet="Log", rows=[["2026-10-14", "run", 30], ["2026-10-15", None, 2.5, True]]
    ).content
    kw = calls(service, "spreadsheets.values.append")[0]
    assert kw == {
        "spreadsheetId": ALIAS_ID,
        "range": "'Log'",
        "valueInputOption": "RAW",
        "insertDataOption": "INSERT_ROWS",
        "body": {"values": [["2026-10-14", "run", 30], ["2026-10-15", "", 2.5, True]]},
    }
    assert out.startswith(f"Appended 2 row(s) to 'Antrenman' ({ALIAS_ID}), tab 'Log' at Log!A5:C6.")
    assert "stored as text (RAW)" in out


def test_append_rows_confirm_preview_shows_target_and_exact_rows(service, ctx):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Koşu")]}
    tool = gs.AppendRows(ctx)
    args = tool.parse_args({"spreadsheet": "Koşu", "sheet": "My Tab", "rows": [["a", 1, None], ["=SUM(A1:A2)", False]]})
    preview = tool.confirm_preview(args)
    assert preview.splitlines() == [
        f"Append 2 row(s) to spreadsheet 'Koşu' ({ID_A}), tab 'My Tab'",
        "Rows:",
        "a\t1\t",
        "=SUM(A1:A2)\tFALSE",
        "Storage: stored as text (RAW); 1 value(s) starting with =, +, - or @ were stored as text",
    ]
    # the run that follows reuses the resolution: one Drive search in total
    tool.run(args)
    assert len(calls(service, "files.list")) == 1
    assert calls(service, "spreadsheets.values.append")[0]["range"] == "'My Tab'"
    # a later run with the same argument resolves again (the memo is consumed)
    tool.run(args)
    assert len(calls(service, "files.list")) == 2


def test_append_rows_preview_asks_once_for_an_ambiguous_name(service, config, paths):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe"), drive_file(ID_B, "Bütçe")]}
    sink = RecordingSink(choice_answer=ID_A)
    tool = gs.AppendRows(make_ctx(config, paths, sink=sink))
    args = tool.parse_args({"spreadsheet": "Bütçe", "sheet": "Log", "rows": [["x"]]})
    assert f"({ID_A})" in tool.confirm_preview(args)
    tool.run(args)
    assert len(sink.choices) == 1 and calls(service, "spreadsheets.values.append")[0]["spreadsheetId"] == ID_A


def test_resolution_memo_does_not_survive_a_denied_write_or_another_turn(service, config, paths):
    """Review fix: a choice made for a confirmation that was then denied (or that belonged to an
    earlier turn) must not be reused silently; the ambiguous name is asked again."""
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Bütçe"), drive_file(ID_B, "Bütçe")]}
    sink = RecordingSink(choice_answer=ID_A)
    ctx = make_ctx(config, paths, sink=sink)
    ctx.runtime.turn_id = "turn-1"
    tool = gs.AppendRows(ctx)
    args = tool.parse_args({"spreadsheet": "Bütçe", "sheet": "Log", "rows": [["x"]]})
    tool.confirm_preview(args)  # the user is asked, picks A, then denies the confirmation: run() never happens
    assert len(sink.choices) == 1
    ctx.runtime.turn_id = "turn-2"  # a later turn, same argument
    sink.choice_answer = ID_B
    tool.confirm_preview(args)
    tool.run(args)
    assert len(sink.choices) == 2 and calls(service, "spreadsheets.values.append")[0]["spreadsheetId"] == ID_B
    # same turn, the memo is consumed by run: a second run in the same turn asks again
    tool.run(args)
    assert len(sink.choices) == 3


def test_short_name_that_looks_like_an_id_is_searched_by_name(service, ctx):
    service.responses["files.list"] = {"files": [drive_file(ID_A, "Antrenman_Programi_2026")]}
    run(gs.ListSheets, ctx, spreadsheet="Antrenman_Programi_2026")
    assert len(calls(service, "files.list")) == 1
    assert calls(service, "spreadsheets.get")[0]["spreadsheetId"] == ID_A


def test_append_rows_limits_and_tab_name_validation(service, ctx):
    with pytest.raises(ToolValidationError, match="at most 200 rows"):
        run(gs.AppendRows, ctx, spreadsheet=ID_A, sheet="Log", rows=[["x"]] * 201)
    with pytest.raises(ToolValidationError, match="at most 50 columns"):
        run(gs.AppendRows, ctx, spreadsheet=ID_A, sheet="Log", rows=[["x"] * 51])
    with pytest.raises(ToolValidationError, match="at least one row"):
        run(gs.AppendRows, ctx, spreadsheet=ID_A, sheet="Log", rows=[])
    with pytest.raises(ToolValidationError, match="not a valid tab name"):
        run(gs.AppendRows, ctx, spreadsheet=ID_A, sheet="Log!A1", rows=[["x"]])
    with pytest.raises(ToolValidationError, match="invalid arguments"):
        run(gs.AppendRows, ctx, spreadsheet=ID_A, sheet="Log", rows=[[{"a": 1}]])
    assert calls(service, "spreadsheets.values.append") == []


# --- update_range -------------------------------------------------------------------------------------


def test_update_range_preview_shows_old_and_new_values(service, ctx):
    service.responses["spreadsheets.values.get"] = {"range": "Log!B2:C3", "values": [["run", "30"], ["bike"]]}
    tool = gs.UpdateRange(ctx)
    args = tool.parse_args({"spreadsheet": ID_A, "range": "Log!B2:C3", "values": [["swim", 45], ["bike", 60]]})
    preview = tool.confirm_preview(args)
    assert preview.splitlines() == [
        f"Update range Log!B2:C3 of spreadsheet 'Antrenman' ({ID_A})",
        "Old:",
        "run\t30",
        "bike",
        "New:",
        "swim\t45",
        "bike\t60",
        "Storage: stored as text (RAW)",
    ]
    service.responses["spreadsheets.values.update"] = {"updatedCells": 4}
    out = tool.run(args).content
    kw = calls(service, "spreadsheets.values.update")[0]
    assert kw == {"spreadsheetId": ID_A, "range": "Log!B2:C3", "valueInputOption": "RAW", "body": {"values": [["swim", 45], ["bike", 60]]}}
    assert out.startswith(f"Updated 4 cell(s) in 'Antrenman' ({ID_A}), range Log!B2:C3.")


def test_update_range_preview_survives_a_failed_read(service, ctx):
    service.responses["spreadsheets.values.get"] = http_error(403, "The caller does not have permission")
    tool = gs.UpdateRange(ctx)
    args = tool.parse_args({"spreadsheet": ID_A, "range": "Log!B2", "values": [["x"]]})
    preview = tool.confirm_preview(args)
    assert "Old:\n(could not read: reading the current values: forbidden (403)" in preview
    assert "New:\nx" in preview


def test_update_range_preview_with_empty_old_range(service, ctx):
    service.responses["spreadsheets.values.get"] = {"range": "Log!Z99"}
    tool = gs.UpdateRange(ctx)
    preview = tool.confirm_preview(tool.parse_args({"spreadsheet": ID_A, "range": "Log!Z99", "values": [["x"]]}))
    assert "Old:\n(empty)\nNew:\nx" in preview


# --- create_spreadsheet -----------------------------------------------------------------------------------


def test_create_spreadsheet_body_preview_and_result(service, ctx):
    new_id = "1NewNewNewNewNewNewNewNewNewNewNewNew000"
    service.responses["spreadsheets.create"] = {
        "spreadsheetId": new_id,
        "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{new_id}/edit",
    }
    tool = gs.CreateSpreadsheet(ctx)
    args = tool.parse_args({"title": "  Bütçe   2027 ", "sheets": ["Gelir", "Gider"]})
    assert tool.confirm_preview(args) == "Create spreadsheet 'Bütçe 2027' with tab(s): Gelir, Gider"
    out = tool.run(args).content
    assert calls(service, "spreadsheets.create")[0]["body"] == {
        "properties": {"title": "Bütçe 2027"},
        "sheets": [{"properties": {"title": "Gelir"}}, {"properties": {"title": "Gider"}}],
    }
    assert out == f"Created spreadsheet 'Bütçe 2027': id {new_id}, URL https://docs.google.com/spreadsheets/d/{new_id}/edit"
    # default tab
    args = tool.parse_args({"title": "Plain"})
    assert tool.confirm_preview(args).endswith("Sheet1 (default)")
    tool.run(args)
    assert calls(service, "spreadsheets.create")[-1]["body"] == {"properties": {"title": "Plain"}}


def test_create_spreadsheet_validation(service, ctx):
    with pytest.raises(ToolValidationError, match="title must be"):
        run(gs.CreateSpreadsheet, ctx, title=" ")
    with pytest.raises(ToolValidationError, match="unique"):
        run(gs.CreateSpreadsheet, ctx, title="x", sheets=["A", "a"])
    with pytest.raises(ToolValidationError, match="at most 20 tabs"):
        run(gs.CreateSpreadsheet, ctx, title="x", sheets=[f"t{i}" for i in range(21)])
    assert calls(service, "spreadsheets.create") == []


# --- doctor -------------------------------------------------------------------------------------------------


def test_health_checks_cover_scopes_and_aliases(monkeypatch, config, paths):
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get_secret", lambda name: store.get(name))
    config.sheets.aliases = {"antrenman": ALIAS_ID}

    def one():
        checks = gs.health_checks(config, paths)
        assert [c.name for c in checks] == ["Google Sheets"]
        return checks[0]

    c = one()
    assert c.ok and c.warn and "no Google token" in c.detail and "1 alias(es)" in c.detail  # optional, not set up
    store["google_token"] = '{"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}'
    c = one()
    assert not c.ok and "jarvis google login` once more" in c.detail and "spreadsheets, drive.metadata.readonly" in c.detail
    store["google_token"] = '{"scopes": %s}' % str(google_auth.SCOPES).replace("'", '"')
    c = one()
    assert c.ok and c.detail == "token covers spreadsheets, drive.metadata.readonly; 1 alias(es) configured in [sheets]"


def test_health_checks_never_raise(monkeypatch, config, paths):
    def boom(name):
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr(secrets, "get_secret", boom)
    checks = gs.health_checks(config, paths)
    assert len(checks) == 1 and not checks[0].ok and "RuntimeError" in checks[0].detail
