"""sheet_table: Turkish numbers and dates, ragged rows padded and labeled, queries computed in Python.

All data here is synthetic. The shape mirrors the real incident: a header row whose first cell is
empty (the date column has no name), rows that end early because the API omits trailing empty
cells, and a balance column next to an income column that a model confused when counting tabs.
"""

from __future__ import annotations

from datetime import date

import pytest

from jarvis.utils import sheet_table as st

TODAY = date(2026, 10, 17)


def serial(d: date) -> float:
    return float((d - st.SHEETS_EPOCH).days)


def date_cell(text: str, d: date) -> st.RawCell:
    return st.RawCell(text, serial(d), is_date=True)


def num(text: str, value: float | None = None) -> st.RawCell:
    return st.RawCell(text, float(text) if value is None else value)


def txt(text: str) -> st.RawCell:
    return st.RawCell(text, text)


def budget_grid() -> list[list[st.RawCell]]:
    """Synthetic cash-flow tab: row 1 empty, header on row 2 with an empty first cell, ragged rows."""
    return [
        [],
        [st.RawCell(""), txt("Bakiye"), txt("Gelir"), txt("Nereden"), txt("Gider"), txt("Nereye")],
        [date_cell("14 Ekim", date(2026, 10, 14)), num("2000"), num("50.000", 50000), txt("fon"), num("52000"), txt("taksit")],
        [date_cell("15 Ekim", date(2026, 10, 15)), num("0")],
        [date_cell("16 Ekim", date(2026, 10, 16)), num("0"), num("67.500", 67500), txt("fon"), num("67500"), txt("ing kk")],
        [date_cell("17 Ekim", date(2026, 10, 17)), num("0")],
        [date_cell("18 Ekim", date(2026, 10, 18)), num("0"), num("30000"), txt("fon")],
        [date_cell("19 Ekim", date(2026, 10, 19)), num("30000"), num("20000"), txt("Fon")],  # balance 30000 is NOT income
        [date_cell("20 Ekim", date(2026, 10, 20)), num("50000")],
        [date_cell("21 Ekim", date(2026, 10, 21)), num("50000"), st.RawCell(""), st.RawCell(""), num("12000"), txt("HARCAMA DÜZELTME")],
        [date_cell("26 Ekim", date(2026, 10, 26)), num("24000"), num("75000"), txt("fon")],
        [date_cell("31 Ekim", date(2026, 10, 31)), num("99000"), num("138000"), txt("maaş")],
        [date_cell("19 Ocak", date(2027, 1, 19)), num("346000")],
    ]


@pytest.fixture
def table() -> st.Table:
    return st.build_table(budget_grid(), today=TODAY)


# --- numbers -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("30.000", 30000),
        ("1,5", 1.5),
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1.5", 1.5),
        ("3.14", 3.14),
        ("-2.000", -2000),
        ("(1.000)", -1000),
        ("₺30.000", 30000),
        ("30.000 TL", 30000),
        ("30 000", 30000),
        ("%12", 12),
        ("12%", 12),
        ("1.234.567", 1234567),
        ("1,234,567", 1234567),
        ("7.1", 7.1),
        ("0", 0),
        ("72000", 72000),
        ("+5", 5),
        ("12,50 €", 12.5),
    ],
)
def test_parse_number_turkish_first(text, expected):
    assert st.parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "   ", "abc", "400 eur borç", "2g altın", "60m", "1.23.4", "1..2", "-", "fon", "21 Eylül", None])
def test_parse_number_rejects_text(text):
    assert st.parse_number(text) is None


def test_format_number():
    assert st.format_number(30000.0) == "30000" and st.format_number(1.5) == "1.5" and st.format_number(-2.0) == "-2"


# --- dates --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-09-21", date(2026, 9, 21)),
        ("2026-9-1 10:30", date(2026, 9, 1)),
        ("21.09.2026", date(2026, 9, 21)),
        ("21/9/26", date(2026, 9, 21)),
        ("21-09-2026", date(2026, 9, 21)),
        ("21 Eylül 2026", date(2026, 9, 21)),
        ("21 eylül", date(2026, 9, 21)),
        ("21 EYLÜL", date(2026, 9, 21)),
        ("Eylül 21, 2026", date(2026, 9, 21)),
        ("Sep 21", date(2026, 9, 21)),
        ("September 21, 2026", date(2026, 9, 21)),
        ("Pazartesi, 21 Eylül", date(2026, 9, 21)),
        ("1 Kasım", date(2026, 11, 1)),
        ("18 Ekim", date(2026, 10, 18)),
        ("21.09", date(2026, 9, 21)),
        # No year: the occurrence nearest to today, across the year boundary in both directions.
        ("19 Ocak", date(2027, 1, 19)),
        ("15 Aralık", date(2026, 12, 15)),
        ("1 Mayıs", date(2026, 5, 1)),
    ],
)
def test_parse_date_turkish_and_english(text, expected):
    assert st.parse_date(text, today=TODAY) == expected


@pytest.mark.parametrize("text", ["", "fon", "30.000", "30 Şubat", "32.01.2026", "2026-13-01", "Ekim", "maaş 2026", None])
def test_parse_date_rejects(text):
    assert st.parse_date(text, today=TODAY) is None


def test_nearest_year_from_a_january_today():
    assert st.parse_date("28 Aralık", today=date(2027, 1, 5)) == date(2026, 12, 28)
    assert st.parse_date("3 Ocak", today=date(2026, 12, 30)) == date(2027, 1, 3)


def test_serial_to_date():
    assert st.serial_to_date(46286) == date(2026, 9, 21)
    assert st.serial_to_date(46286.75) == date(2026, 9, 21)  # a DATE_TIME serial keeps the day
    assert st.serial_to_date(1) == date(1899, 12, 31)
    assert st.serial_to_date(0) is None and st.serial_to_date(-5) is None and st.serial_to_date("x") is None


# --- cells ----------------------------------------------------------------------------------------------


def test_make_cell_date_formatted_cell_uses_the_serial_for_the_real_date():
    cell = st.make_cell(date_cell("19 Ekim", date(2026, 10, 19)), today=TODAY)
    assert cell.kind == "date" and cell.date == date(2026, 10, 19) and cell.text == "19 Ekim"
    assert cell.label() == '2026-10-19 ("19 Ekim")'


def test_make_cell_numbers_text_and_display():
    assert st.make_cell(num("30000"), today=TODAY).label() == "30000"
    assert st.make_cell(num("30.000", 30000), today=TODAY).label() == '30000 ("30.000")'
    assert st.make_cell(st.RawCell("1,5", 1.5), today=TODAY).label() == '1.5 ("1,5")'
    assert st.make_cell(txt("fon"), today=TODAY).label() == '"fon"'
    assert st.make_cell(st.RawCell("TRUE", True), today=TODAY).label() == "TRUE"
    assert st.make_cell(st.RawCell(""), today=TODAY).label() == "(empty)" and st.make_cell(st.RawCell(""), today=TODAY).empty


def test_make_cell_parses_text_cells_from_their_display():
    # A text cell (no effective number) with Turkish formatting still becomes a number; a text date becomes a date.
    assert st.make_cell(st.RawCell("30.000", "30.000"), today=TODAY).number == 30000
    assert st.make_cell(st.RawCell("1,5", "1,5"), today=TODAY).number == 1.5
    assert st.make_cell(st.RawCell("21 Eylül", "21 Eylül"), today=TODAY).date == date(2026, 9, 21)
    assert st.make_cell(st.RawCell("400 eur borç", "400 eur borç"), today=TODAY).kind == "text"


def test_make_cell_date_serial_without_a_reported_format_type():
    # The cell is a date (its display parses as one) but the API did not report numberFormat: the serial wins.
    cell = st.make_cell(st.RawCell("19 Ekim", serial(date(2026, 10, 19))), today=TODAY)
    assert cell.date == date(2026, 10, 19)


def test_make_cell_cleans_the_displayed_text():
    cell = st.make_cell(st.RawCell("a​b\tc\nd", "x"), today=TODAY, clean=lambda s: s.replace("​", ""))
    assert cell.text == "ab c d"


# --- table ------------------------------------------------------------------------------------------------


def test_build_table_pads_ragged_rows_and_labels_by_header(table):
    assert [c.header for c in table.columns] == ["A", "Bakiye", "Gelir", "Nereden", "Gider", "Nereye"]
    assert [c.kind for c in table.columns] == ["date", "number", "number", "text", "number", "text"]
    assert table.header_row == 2 and not table.columns[0].named and table.columns[1].named
    assert [r.number for r in table.rows] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]  # sheet row numbers, row 1 empty skipped
    assert all(len(r.cells) == 6 for r in table.rows)
    rec = table.record(table.rows[5])  # 19 Ekim: the row that was misread
    assert rec["A"].date == date(2026, 10, 19)
    assert rec["Bakiye"].number == 30000 and rec["Gelir"].number == 20000 and rec["Nereden"].text == "Fon"
    assert rec["Gider"].empty and rec["Nereye"].empty


def test_row_and_columns_lines(table):
    assert st.row_line(table, table.rows[5]) == (
        'Row 8: A=2026-10-19 ("19 Ekim") | Bakiye=30000 | Gelir=20000 | Nereden="Fon" | Gider=(empty) | Nereye=(empty)'
    )
    assert st.row_line(table, table.rows[1]) == (
        'Row 4: A=2026-10-15 ("15 Ekim") | Bakiye=0 | Gelir=(empty) | Nereden=(empty) | Gider=(empty) | Nereye=(empty)'
    )
    line = st.columns_line(table)
    assert line.startswith(
        "Columns: A [date, no header] | Bakiye [number] | Gelir [number] | Nereden [text] | Gider [number] | Nereye [text]"
    )
    assert "header on sheet row 2" in line and "named by its letter" in line


def test_empty_middle_cells_keep_their_columns(table):
    rec = table.record(table.rows[7])  # 21 Ekim: two explicit blanks before Gider
    assert rec["Gelir"].empty and rec["Nereden"].empty and rec["Gider"].number == 12000 and rec["Nereye"].text == "HARCAMA DÜZELTME"


def test_build_table_without_a_header_names_columns_by_letter():
    grid = [[num("1"), num("2")], [num("3"), num("4"), num("5")]]
    t = st.build_table(grid, today=TODAY)
    assert t.header_row is None and [c.header for c in t.columns] == ["A", "B", "C"]
    assert [r.number for r in t.rows] == [1, 2] and len(t.rows[0].cells) == 3
    assert "no header row detected" in st.columns_line(t)


def test_build_table_header_forced_and_duplicate_headers_and_offsets():
    grid = [[txt("Ad"), txt("Ad"), txt("ad")], [txt("x"), txt("y"), txt("z")]]
    t = st.build_table(grid, first_row=5, first_column=2, today=TODAY, header=True)
    assert [c.letter for c in t.columns] == ["C", "D", "E"]
    assert [c.header for c in t.columns] == ["Ad", "Ad (2)", "ad (3)"]
    assert t.header_row == 5 and t.rows[0].number == 6


def test_build_table_drops_empty_rows_and_a_text_only_first_row_is_the_header():
    grid = [[txt("Tarih"), txt("Not")], [], [date_cell("1 Ekim", date(2026, 10, 1)), txt("a")], [st.RawCell(""), st.RawCell("")]]
    t = st.build_table(grid, today=TODAY)
    assert t.header_row == 1 and [r.number for r in t.rows] == [3]


def test_table_column_lookup_is_turkish_case_insensitive_and_accepts_letters(table):
    assert table.column("gelİr").header == "Gelir"
    assert table.column("NEREDEN").header == "Nereden"
    assert table.column("a").letter == "A" and table.column("c").header == "Gelir"
    with pytest.raises(KeyError):  # ambiguous partial ("Nereden" / "Nereye") must not resolve
        table.column("nere")
    with pytest.raises(KeyError, match="no column named 'Tutar'"):
        table.column("Tutar")


def test_column_letter():
    assert [st.column_letter(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702)] == ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]


def test_grid_from_api_reads_effective_values_and_date_formats():
    sheet_data = {
        "startRow": 1,
        "startColumn": 0,
        "rowData": [
            {},
            {"values": [{}, {"formattedValue": "Gelir", "effectiveValue": {"stringValue": "Gelir"}}]},
            {
                "values": [
                    {
                        "formattedValue": "19 Ekim",
                        "effectiveValue": {"numberValue": 46314},
                        "effectiveFormat": {"numberFormat": {"type": "DATE"}},
                    },
                    {"formattedValue": "30.000", "effectiveValue": {"numberValue": 30000}},
                    {"formattedValue": "TRUE", "effectiveValue": {"boolValue": True}},
                    {"formattedValue": "#DIV/0!", "effectiveValue": {"errorValue": {"type": "DIVIDE_BY_ZERO"}}},
                ]
            },
        ],
    }
    grid, first_row, first_col = st.grid_from_api(sheet_data)
    assert (first_row, first_col) == (2, 0)
    assert grid[0] == [] and grid[1][0] == st.RawCell("", None, False)
    assert grid[2][0] == st.RawCell("19 Ekim", 46314.0, True)
    assert grid[2][1] == st.RawCell("30.000", 30000.0, False)
    assert grid[2][2] == st.RawCell("TRUE", True, False)
    assert grid[2][3] == st.RawCell("#DIV/0!", None, False)
    t = st.build_table(grid, first_row=first_row, first_column=first_col, today=TODAY)
    assert t.header_row == 3 and t.rows[0].number == 4 and t.rows[0].cells[0].date == date(2026, 10, 19)


def test_grid_from_values_and_table_from_values():
    t = st.table_from_values([["Tarih", "Tutar"], ["21 Eylül", "30.000"], ["22 Eylül"]], today=TODAY)
    assert [c.header for c in t.columns] == ["Tarih", "Tutar"]
    assert t.rows[0].cells[0].date == date(2026, 9, 21) and t.rows[0].cells[1].number == 30000
    assert t.rows[1].cells[1].empty


def test_render_rows_respects_the_cap_and_reports_the_cut(table):
    lines, kept = st.render_rows(table, table.rows, cap=260, more_hint="; narrow it")
    assert kept < len(table.rows) and kept >= 1
    assert lines[-1] == f"[{len(table.rows) - kept} more row(s) not shown; narrow it]"
    lines, kept = st.render_rows(table, table.rows, cap=100_000)
    assert kept == len(table.rows) and len(lines) == len(table.rows)


# --- queries ----------------------------------------------------------------------------------------------


def test_query_fon_rows_after_today_sorted_with_sum(table):
    result = st.query(
        table,
        filters=[st.Filter("Nereden", "contains", "fon"), st.Filter("A", "gte", "today")],
        sort_by="A",
        aggregate="sum",
        aggregate_column="Gelir",
        today=TODAY,
    )
    assert [r.number for r in result.rows] == [7, 8, 11]  # 18, 19, 26 Ekim; the 14 and 16 Ekim rows are in the past
    assert result.filters == ['Nereden contains "fon"', "A gte 2026-10-17 (today = 2026-10-17)"]
    assert result.sort == "A ascending"
    assert result.aggregate_value == 125000
    assert result.aggregate == "sum of Gelir over 3 matched row(s) = 125000 (3 numeric cell(s), 0 empty or non-numeric skipped)"


def test_query_next_upcoming_entry_is_the_first_row_after_sorting(table):
    result = st.query(table, filters=[st.Filter("Nereden", "contains", "FON"), st.Filter("A", "gt", "bugün")], sort_by="A", today=TODAY)
    first = table.record(result.rows[0])
    assert first["A"].date == date(2026, 10, 18) and first["Gelir"].number == 30000
    second = table.record(result.rows[1])
    assert second["A"].date == date(2026, 10, 19) and second["Gelir"].number == 20000  # not the 30000 balance


def test_query_sum_of_the_two_disputed_rows(table):
    result = st.query(
        table,
        filters=[st.Filter("Nereden", "contains", "fon"), st.Filter("A", "gte", "2026-10-18"), st.Filter("A", "lte", "19 Ekim")],
        aggregate="sum",
        aggregate_column="Gelir",
        today=TODAY,
    )
    assert [r.number for r in result.rows] == [7, 8] and result.aggregate_value == 50000


def test_query_relative_days_numbers_text_and_empty_ops(table):
    assert [r.number for r in st.query(table, filters=[st.Filter("A", "equals", "today+1")], today=TODAY).rows] == [7]
    assert [r.number for r in st.query(table, filters=[st.Filter("A", "lt", "today-1")], today=TODAY).rows] == [3, 4]
    assert [r.number for r in st.query(table, filters=[st.Filter("Gelir", "gte", "67.500")], today=TODAY).rows] == [5, 11, 12]
    assert [r.number for r in st.query(table, filters=[st.Filter("Gelir", "is_empty")], today=TODAY).rows] == [4, 6, 9, 10, 13]
    assert [r.number for r in st.query(table, filters=[st.Filter("Gider", "not_empty")], today=TODAY).rows] == [3, 5, 10]
    assert [r.number for r in st.query(table, filters=[st.Filter("Nereye", "equals", "harcama düzeltme")], today=TODAY).rows] == [10]
    assert [
        r.number
        for r in st.query(table, filters=[st.Filter("Nereden", "not_contains", "fon"), st.Filter("Gelir", "not_empty")], today=TODAY).rows
    ] == [12]
    assert [r.number for r in st.query(table, filters=[st.Filter("Bakiye", "equals", "30.000")], today=TODAY).rows] == [8]


def test_query_count_avg_min_max_and_descending_sort(table):
    assert st.query(table, aggregate="count", today=TODAY).aggregate == "count of matched rows = 11"
    r = st.query(table, filters=[st.Filter("Nereden", "contains", "fon")], aggregate="count", aggregate_column="Gelir", today=TODAY)
    assert r.aggregate == "count of Gelir over 5 matched row(s) = 5 numeric cell(s)"
    r = st.query(table, filters=[st.Filter("Nereden", "contains", "fon")], aggregate="avg", aggregate_column="Gelir", today=TODAY)
    assert r.aggregate_value == pytest.approx((50000 + 67500 + 30000 + 20000 + 75000) / 5)
    assert st.query(table, aggregate="max", aggregate_column="Gelir", today=TODAY).aggregate_value == 138000
    assert st.query(table, aggregate="min", aggregate_column="Bakiye", today=TODAY).aggregate_value == 0
    r = st.query(table, sort_by="Gelir", descending=True, today=TODAY)
    assert [c.number for c in (row.cells[2] for row in r.rows[:3])] == [138000, 75000, 67500]
    assert r.rows[-1].cells[2].empty  # empty cells sort last either way
    r = st.query(table, filters=[st.Filter("Nereye", "not_empty")], aggregate="sum", aggregate_column="Nereye", today=TODAY)
    assert r.aggregate_value is None and r.aggregate.endswith("no numeric cells")


def test_query_errors(table):
    with pytest.raises(KeyError, match="no column named 'Tutar'"):
        st.query(table, filters=[st.Filter("Tutar", "gt", "1")], today=TODAY)
    with pytest.raises(ValueError, match="unknown filter op"):
        st.query(table, filters=[st.Filter("Gelir", "like", "1")], today=TODAY)
    with pytest.raises(ValueError, match="needs a value"):
        st.query(table, filters=[st.Filter("Gelir", "gt", "")], today=TODAY)
    with pytest.raises(ValueError, match="unknown aggregate"):
        st.query(table, aggregate="median", aggregate_column="Gelir", today=TODAY)
    with pytest.raises(ValueError, match="needs a column"):
        st.query(table, aggregate="sum", today=TODAY)


def test_parse_filter_value_kinds():
    assert st.parse_filter_value("today", today=TODAY) == st.FilterValue("date", date=TODAY, note="today = 2026-10-17")
    assert st.parse_filter_value("bugün + 7", today=TODAY).date == date(2026, 10, 24)
    assert st.parse_filter_value("yarın", today=TODAY).date == date(2026, 10, 18)
    assert st.parse_filter_value("dün", today=TODAY).date == date(2026, 10, 16)
    assert st.parse_filter_value("19 Ekim", today=TODAY) == st.FilterValue("date", date=date(2026, 10, 19))
    assert st.parse_filter_value("30.000", today=TODAY) == st.FilterValue("number", number=30000.0)
    assert st.parse_filter_value("fon", today=TODAY) == st.FilterValue("text", text="fon")
    assert st.parse_filter_value("", today=TODAY).kind == "none"


def test_fold_handles_turkish_i():
    assert st.fold("FON") == "fon" and st.fold("İSTANBUL") == "istanbul" and st.fold("ISPARTA") == "isparta"
    assert st.fold("KASIM") == st.fold("kasım") == st.fold("Kasim")  # every i-like letter is the same in both languages
    assert st.fold("  Harcama   Düzeltme ") == "harcama düzeltme"
