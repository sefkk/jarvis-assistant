"""Spreadsheet cells as a clean table, and table questions answered in Python.

Why this exists: the Sheets API returns ragged rows (trailing empty cells are omitted, an empty
row is `[]`), and a model reading tab-separated lines miscounts the columns. On a four-cell row
such as `19 Ekim <tab> 30000 <tab> 20000 <tab> fon` it took the running balance (column B) for the
income (column C) and then did the arithmetic in its head. This module:

- pads every row to the header's width and labels each cell with its column header
  (`Row 29: A=2026-10-19 ("19 Ekim") | Gelir=20000 | Nereden="fon" | ...`);
- keeps both the displayed text and a parsed value: dates as real dates (from the sheet's serial
  number when the cell is date-formatted, otherwise from Turkish or English text such as
  "21 Eylül", "21.09.2026", "2026-09-21"), numbers from Turkish text ("30.000" is thirty
  thousand, "1,5" is one point five, "1.234,56", "₺30.000", "30.000 TL");
- filters, sorts and sums (`query`) so the model never adds numbers or compares dates itself.

Nothing here touches the network or logs cell content.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from jarvis.utils.text import fold as fold_letters
from jarvis.utils.timeutil import now_local

# --- case-insensitive text -----------------------------------------------------------------------------


def fold(text: str) -> str:
    """Case-insensitive comparison key, the same in English and Turkish: every i-like letter is a plain "i"
    ("FON" == "fon", "İSTANBUL" == "istanbul", "KASIM" == "kasim"). Whitespace is collapsed."""
    return " ".join(fold_letters(unicodedata.normalize("NFKC", text or "")).split())


# --- numbers --------------------------------------------------------------------------------------

_CURRENCY_RE = re.compile(r"(?i)[₺$€£]|\b(?:tl|try|eur|usd|gbp|euro|dolar|lira)\b")
_PERCENT_RE = re.compile(r"%")
_NUMBER_BODY_RE = re.compile(r"^[+-]?[\d.,]+$")


def parse_number(text: str) -> float | None:
    """A number from displayed text, Turkish rules first.

    "30.000" -> 30000 (a dot followed by exactly three digits is a thousands separator),
    "1,5" -> 1.5 (the comma is the decimal mark), "1.234,56" -> 1234.56, "1,234.56" -> 1234.56
    (the last separator is the decimal mark when both appear), "1.5" -> 1.5, "3.14" -> 3.14,
    "-2.000" -> -2000, "(1.000)" -> -1000, "₺30.000", "30.000 TL", "%12", "12%" -> 12.
    Anything with other letters ("400 eur borç", "2g altın", "60m") is not a number."""
    if text is None:
        return None
    s = unicodedata.normalize("NFKC", str(text)).strip()
    if not s:
        return None
    s = _CURRENCY_RE.sub("", s)
    s = _PERCENT_RE.sub("", s)
    s = s.replace(" ", " ").replace(" ", "")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if not _NUMBER_BODY_RE.match(s):
        return None
    if s[0] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:]
    if not s or not s[0].isdigit() or not s[-1].isdigit():
        return None
    dots, commas = s.count("."), s.count(",")
    try:
        if dots and commas:
            # The last separator is the decimal mark; the other one groups thousands.
            if s.rfind(",") > s.rfind("."):
                value = float(s.replace(".", "").replace(",", "."))
            else:
                value = float(s.replace(",", ""))
        elif commas:
            # Turkish: the comma is the decimal mark. Several comma groups ("1,234,567") are English thousands.
            value = float(s.replace(",", "")) if commas > 1 else float(s.replace(",", "."))
        elif dots:
            groups = s.split(".")
            if dots > 1 or (len(groups[1]) == 3 and len(groups[0]) <= 3):
                if not all(len(g) == 3 for g in groups[1:]):
                    return None
                value = float("".join(groups))
            else:
                value = float(s)
        else:
            value = float(s)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return -value if negative else value


def format_number(value: float) -> str:
    """Numbers for the model: integers without a decimal part, otherwise a plain decimal point."""
    if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 10))


# --- dates -----------------------------------------------------------------------------------------

SHEETS_EPOCH = date(1899, 12, 30)  # Google Sheets serial 1 is 1899-12-31

_MONTHS = {
    "ocak": 1,
    "şubat": 2,
    "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayıs": 5,
    "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "ağustos": 8,
    "agustos": 8,
    "eylül": 9,
    "eylul": 9,
    "ekim": 10,
    "kasım": 11,
    "kasim": 11,
    "aralık": 12,
    "aralik": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "oca": 1,
    "şub": 2,
    "nis": 4,
    "haz": 6,
    "tem": 7,
    "ağu": 8,
    "eyl": 9,
    "eki": 10,
    "kas": 11,
    "ara": 12,
}
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_DMY_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?(?:\s.*)?$")
_D_MONTH_RE = re.compile(r"^(\d{1,2})\.?\s+([^\W\d_]+)\.?(?:\s+(\d{4}))?(?:\s.*)?$")
_MONTH_D_RE = re.compile(r"^([^\W\d_]+)\.?\s+(\d{1,2}),?(?:\s+(\d{4}))?$")
_WEEKDAY_RE = re.compile(
    r"(?i)^(?:pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|cumartesi|pazar|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|pzt|sal|çar|per|cum|cmt|paz|mon|tue|wed|thu|fri|sat|sun),?\s+"
)


def today_local() -> date:
    """Today in the Mac's time zone. Tests and the eval runner replace this."""
    return now_local().date()


def serial_to_date(serial: float) -> date | None:
    """A Google Sheets date serial (days since 1899-12-30) as a date; None when implausible."""
    try:
        days = int(math.floor(float(serial)))
    except (TypeError, ValueError, OverflowError):
        return None
    if days < 1 or days > 2_958_465:  # 9999-12-31
        return None
    return SHEETS_EPOCH + timedelta(days=days)


def _nearest_year(month: int, day: int, today: date) -> date | None:
    """A day without a year ("19 Ocak") is the occurrence closest to today, so a sheet that runs
    from September into January keeps its order across the year boundary."""
    best: date | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best


def _make_date(year: int | None, month: int, day: int, today: date) -> date | None:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    if year is None:
        return _nearest_year(month, day, today)
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str, *, today: date | None = None) -> date | None:
    """A date from displayed text: "2026-09-21", "21.09.2026", "21/9/26", "21.09" (nearest year),
    "21 Eylül", "21 Eylül 2026", "Eylül 21, 2026", "Sep 21", "Pazartesi, 21 Eylül". None otherwise."""
    if text is None:
        return None
    today = today or today_local()
    s = " ".join(unicodedata.normalize("NFKC", str(text)).split())
    if not s:
        return None
    s = _WEEKDAY_RE.sub("", s)
    m = _ISO_RE.match(s)
    if m:
        return _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
    m = _DMY_RE.match(s)
    if m:
        year = int(m.group(3)) if m.group(3) else None
        return _make_date(year, int(m.group(2)), int(m.group(1)), today)
    m = _D_MONTH_RE.match(s)
    if m:
        month = _MONTHS.get(fold(m.group(2)))
        if month:
            return _make_date(int(m.group(3)) if m.group(3) else None, month, int(m.group(1)), today)
    m = _MONTH_D_RE.match(s)
    if m:
        month = _MONTHS.get(fold(m.group(1)))
        if month:
            return _make_date(int(m.group(3)) if m.group(3) else None, month, int(m.group(2)), today)
    return None


# --- cells and tables ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawCell:
    """One cell as the API reports it: the displayed text, the effective value (number, string or
    bool; None when unknown or empty) and whether the cell is date-formatted (then `value` is a
    serial number)."""

    text: str = ""
    value: str | float | bool | None = None
    is_date: bool = False


@dataclass(frozen=True)
class Cell:
    text: str  # what the user sees (sanitized by the caller), "" when empty
    number: float | None = None
    date: date | None = None
    boolean: bool | None = None

    @property
    def empty(self) -> bool:
        return self.text == "" and self.number is None and self.date is None and self.boolean is None

    @property
    def kind(self) -> str:
        if self.empty:
            return "empty"
        if self.date is not None:
            return "date"
        if self.number is not None:
            return "number"
        if self.boolean is not None:
            return "bool"
        return "text"

    def label(self) -> str:
        """The cell for the model: the parsed value first, the displayed text in quotes when it
        differs, so "30.000" reads as 30000 ("30.000") and "19 Ekim" as 2026-10-19 ("19 Ekim")."""
        if self.empty:
            return "(empty)"
        if self.date is not None:
            iso = self.date.isoformat()
            return iso if self.text in ("", iso) else f'{iso} ("{self.text}")'
        if self.number is not None:
            shown = format_number(self.number)
            return shown if self.text in ("", shown) else f'{shown} ("{self.text}")'
        if self.boolean is not None:
            return "TRUE" if self.boolean else "FALSE"
        return f'"{self.text}"'


EMPTY_CELL = Cell("")


def make_cell(raw: RawCell, *, today: date | None = None, clean=None) -> Cell:
    """Parse one raw cell. `clean` sanitizes the displayed text (the caller passes the model-safe
    cleaner); the parsed value never comes from anything but the cell itself."""
    text = " ".join((clean(raw.text) if clean else str(raw.text or "")).split())
    value = raw.value
    if isinstance(value, bool):
        return Cell(text or ("TRUE" if value else "FALSE"), boolean=value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        if raw.is_date:
            d = serial_to_date(value)
            if d is not None:
                return Cell(text or d.isoformat(), date=d)
        if text and 10_000 <= value <= 100_000 and parse_date(text, today=today) is not None and not _NUMBER_BODY_RE.match(text):
            # A date-formatted cell whose format type was not reported: the serial still gives the real date.
            d = serial_to_date(value)
            if d is not None:
                return Cell(text, date=d)
        return Cell(text or format_number(float(value)), number=float(value))
    if not text:
        return EMPTY_CELL
    number = parse_number(text)
    if number is not None:
        return Cell(text, number=number)
    d = parse_date(text, today=today)
    if d is not None:
        return Cell(text, date=d)
    return Cell(text)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


@dataclass(frozen=True)
class Column:
    letter: str
    header: str  # the header cell's text, or the letter when that cell is empty
    kind: str  # the dominant kind of its data cells: date / number / text / bool / empty

    @property
    def named(self) -> bool:
        return self.header != self.letter


@dataclass
class Row:
    number: int  # the sheet's own 1-based row number, so the model can cite it and update_range can target it
    cells: list[Cell]

    @property
    def empty(self) -> bool:
        return all(c.empty for c in self.cells)


@dataclass
class Table:
    columns: list[Column]
    rows: list[Row] = field(default_factory=list)  # data rows only, padded to len(columns)
    header_row: int | None = None  # sheet row number of the header, None when the range has no header

    def record(self, row: Row) -> dict[str, Cell]:
        return {col.header: cell for col, cell in zip(self.columns, row.cells)}

    def column(self, name: str) -> Column:
        """A column by header (case-insensitive, Turkish-aware, substring as a fallback) or by letter."""
        wanted = fold(name)
        if not wanted:
            raise KeyError("column name is empty")
        for col in self.columns:
            if fold(col.header) == wanted:
                return col
        for col in self.columns:
            if col.letter.lower() == wanted:
                return col
        partial = [col for col in self.columns if col.named and wanted in fold(col.header)]
        if len(partial) == 1:
            return partial[0]
        raise KeyError(f"no column named {name!r}; the columns are: {', '.join(self.describe_columns())}")

    def describe_columns(self) -> list[str]:
        return [f"{c.header} [{c.kind}]" if c.named else f"{c.letter} [{c.kind}, no header]" for c in self.columns]

    def index_of(self, col: Column) -> int:
        return self.columns.index(col)


def _looks_like_header(cells: list[Cell]) -> bool:
    """The first non-empty row is a header when at least half of its filled cells are plain text."""
    filled = [c for c in cells if not c.empty]
    if not filled:
        return False
    texts = sum(1 for c in filled if c.kind == "text")
    return texts * 2 >= len(filled)


def _dominant_kind(cells: Iterable[Cell]) -> str:
    counts: dict[str, int] = {}
    for c in cells:
        if not c.empty:
            counts[c.kind] = counts.get(c.kind, 0) + 1
    if not counts:
        return "empty"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == "text"))[0]


def build_table(
    grid: list[list[RawCell]],
    *,
    first_row: int = 1,
    first_column: int = 0,
    today: date | None = None,
    clean=None,
    header: bool | None = None,
) -> Table:
    """Pad, parse and label a grid of raw cells.

    `first_row` / `first_column` locate the grid in the sheet (1-based row, 0-based column) so row
    numbers and letters are the sheet's own. Leading empty rows are skipped; the first non-empty
    row is the header when `header` is True, or None and it looks like one (mostly text) and
    at least one more row follows. A column whose header cell is empty is named by its letter. Duplicate headers get " (2)",
    " (3)". Entirely empty data rows are dropped."""
    today = today or today_local()
    parsed: list[tuple[int, list[Cell]]] = []
    width = 0
    for offset, raw_row in enumerate(grid):
        cells = [make_cell(c, today=today, clean=clean) for c in (raw_row or [])]
        while cells and cells[-1].empty:
            cells.pop()
        width = max(width, len(cells))
        parsed.append((first_row + offset, cells))
    width = max(width, 1)
    parsed = [(n, cells + [EMPTY_CELL] * (width - len(cells))) for n, cells in parsed]
    while parsed and all(c.empty for c in parsed[0][1]):
        parsed.pop(0)
    header_row: int | None = None
    headers: list[str]
    # A single non-empty row is data, not a header (nothing would be left to describe).
    if parsed and (header is True or (header is None and len(parsed) > 1 and _looks_like_header(parsed[0][1]))):
        header_row, head_cells = parsed.pop(0)
        headers = [c.text if c.kind == "text" else (c.label().strip('"') if not c.empty else "") for c in head_cells]
    else:
        headers = [""] * width
    letters = [column_letter(first_column + i) for i in range(width)]
    names: list[str] = []
    seen: dict[str, int] = {}
    for letter, name in zip(letters, headers):
        name = name or letter
        key = fold(name)
        if key in seen:
            seen[key] += 1
            name = f"{name} ({seen[key]})"
        else:
            seen[key] = 1
        names.append(name)
    rows = [Row(n, cells) for n, cells in parsed if not all(c.empty for c in cells)]
    columns = [Column(letter, name, _dominant_kind(r.cells[i] for r in rows)) for i, (letter, name) in enumerate(zip(letters, names))]
    return Table(columns, rows, header_row)


def grid_from_api(sheet_data: dict[str, Any]) -> tuple[list[list[RawCell]], int, int]:
    """The `sheets[].data[]` entry of `spreadsheets.get(includeGridData=True)` as raw cells plus
    the 1-based first row and 0-based first column of the range."""
    first_row = int(sheet_data.get("startRow") or 0) + 1
    first_column = int(sheet_data.get("startColumn") or 0)
    grid: list[list[RawCell]] = []
    for row_data in sheet_data.get("rowData") or []:
        cells: list[RawCell] = []
        for v in (row_data or {}).get("values") or []:
            v = v or {}
            effective = v.get("effectiveValue") or {}
            value: str | float | bool | None = None
            if "boolValue" in effective:
                value = bool(effective["boolValue"])
            elif "numberValue" in effective and isinstance(effective["numberValue"], (int, float)):
                value = float(effective["numberValue"])
            elif "stringValue" in effective:
                value = str(effective["stringValue"])
            fmt_type = (((v.get("effectiveFormat") or {}).get("numberFormat") or {}).get("type") or "").upper()
            text = v.get("formattedValue")
            cells.append(RawCell(str(text) if text is not None else "", value, fmt_type in ("DATE", "DATE_TIME")))
        grid.append(cells)
    return grid, first_row, first_column


def grid_from_values(values: list[list[Any]]) -> list[list[RawCell]]:
    """Rows from `values.get` (formatted strings only): text cells, parsed from their display."""
    grid: list[list[RawCell]] = []
    for row in values or []:
        cells = []
        for c in row if isinstance(row, list) else []:
            if isinstance(c, bool):
                cells.append(RawCell("TRUE" if c else "FALSE", c))
            elif isinstance(c, (int, float)):
                cells.append(RawCell(format_number(float(c)), float(c)))
            else:
                cells.append(RawCell("" if c is None else str(c)))
        grid.append(cells)
    return grid


# --- rendering for the model ----------------------------------------------------------------------------


def row_line(table: Table, row: Row) -> str:
    cells = " | ".join(f"{col.header}={cell.label()}" for col, cell in zip(table.columns, row.cells))
    return f"Row {row.number}: {cells}"


def columns_line(table: Table) -> str:
    line = "Columns: " + " | ".join(table.describe_columns())
    if table.header_row is not None:
        line += f" (header on sheet row {table.header_row}"
        if any(not c.named for c in table.columns):
            line += "; a column whose header cell is empty is named by its letter"
        line += ")"
    else:
        line += " (no header row detected; columns are named by their letters)"
    return line


def render_rows(table: Table, rows: list[Row], *, cap: int, used: int = 0, more_hint: str = "") -> tuple[list[str], int]:
    """Row lines that fit in `cap` characters (counting `used`); the last line reports the cut."""
    lines: list[str] = []
    kept = 0
    for row in rows:
        line = row_line(table, row)
        if lines and used + len(line) + 1 > cap:
            break
        lines.append(line)
        used += len(line) + 1
        kept += 1
    if kept < len(rows):
        lines.append(f"[{len(rows) - kept} more row(s) not shown{more_hint}]")
    return lines, kept


# --- queries ----------------------------------------------------------------------------------------

FILTER_OPS = ("contains", "not_contains", "equals", "not_equals", "gt", "gte", "lt", "lte", "is_empty", "not_empty")
AGGREGATE_OPS = ("sum", "count", "avg", "min", "max")
_RELATIVE_DAYS = {
    "today": 0,
    "bugün": 0,
    "bugun": 0,
    "now": 0,
    "şimdi": 0,
    "tomorrow": 1,
    "yarın": 1,
    "yarin": 1,
    "yesterday": -1,
    "dün": -1,
    "dun": -1,
}


@dataclass(frozen=True)
class FilterValue:
    kind: str  # date / number / text / none
    date: date | None = None
    number: float | None = None
    text: str = ""
    note: str = ""  # how a relative word was resolved, e.g. "today = 2026-10-17"

    def describe(self) -> str:
        if self.kind == "date":
            return self.date.isoformat() + (f" ({self.note})" if self.note else "")
        if self.kind == "number":
            return format_number(self.number)  # type: ignore[arg-type]
        return f'"{self.text}"'


def parse_filter_value(text: str, *, today: date | None = None) -> FilterValue:
    """What a filter compares against: a relative day ("today", "bugün", "yarın", "today+7",
    "today-30"), a date, a number, or plain text (compared Turkish case-insensitively)."""
    today = today or today_local()
    s = " ".join(str(text or "").split())
    if not s:
        return FilterValue("none")
    m = re.match(r"(?i)^([^\W\d_]+)\s*([+-])\s*(\d{1,4})$", s)
    base = fold(m.group(1) if m else s)
    if base in _RELATIVE_DAYS:
        days = _RELATIVE_DAYS[base] + (int(m.group(3)) * (1 if m.group(2) == "+" else -1) if m else 0)
        d = today + timedelta(days=days)
        return FilterValue("date", date=d, note=f"{s} = {d.isoformat()}")
    d = parse_date(s, today=today)
    if d is not None and not _NUMBER_BODY_RE.match(s):
        return FilterValue("date", date=d)
    n = parse_number(s)
    if n is not None:
        return FilterValue("number", number=n)
    return FilterValue("text", text=s)


def _compare(cell: Cell, value: FilterValue) -> int | None:
    """-1 / 0 / 1 for cell versus value in the value's own type; None when not comparable."""
    if value.kind == "date":
        if cell.date is None:
            return None
        return (cell.date > value.date) - (cell.date < value.date)  # type: ignore[operator]
    if value.kind == "number":
        if cell.number is None:
            return None
        return (cell.number > value.number) - (cell.number < value.number)  # type: ignore[operator]
    if value.kind == "text":
        a, b = fold(cell.text), fold(value.text)
        return (a > b) - (a < b)
    return None


def cell_matches(cell: Cell, op: str, value: FilterValue) -> bool:
    if op == "is_empty":
        return cell.empty
    if op == "not_empty":
        return not cell.empty
    if op in ("contains", "not_contains"):
        hit = bool(value.text) and value.kind == "text" and fold(value.text) in fold(cell.text)
        if value.kind != "text":  # "contains 2026-10" or "contains 30000": match against the label too
            hit = (
                fold(value.describe().strip('"')) in fold(cell.text)
                or (value.kind == "number" and cell.number is not None and cell.number == value.number)
                or (value.kind == "date" and cell.date == value.date)
            )
        return hit if op == "contains" else not hit
    cmp = _compare(cell, value)
    if op == "equals":
        return cmp == 0 if cmp is not None else (fold(cell.text) == fold(value.describe().strip('"')))
    if op == "not_equals":
        return not cell_matches(cell, "equals", value)
    if cmp is None:
        return False
    return {"gt": cmp > 0, "gte": cmp >= 0, "lt": cmp < 0, "lte": cmp <= 0}[op]


@dataclass
class Filter:
    column: str
    op: str
    value: str = ""


@dataclass
class QueryResult:
    rows: list[Row]
    filters: list[str]  # human-readable, e.g. 'Nereden contains "fon"'
    sort: str | None
    aggregate: str | None  # e.g. "sum of Gelir over 9 matched row(s) = 487500 (9 numeric cell(s), 0 skipped)"
    aggregate_value: float | None = None
    today: date | None = None


def _sort_key(cell: Cell):
    if cell.date is not None:
        return (0, cell.date.toordinal(), "")
    if cell.number is not None:
        return (1, cell.number, "")
    return (2, 0, fold(cell.text))


def query(
    table: Table,
    *,
    filters: list[Filter] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    aggregate: str | None = None,
    aggregate_column: str | None = None,
    today: date | None = None,
) -> QueryResult:
    """Filter (all filters must hold), sort, and aggregate in Python.

    Raises KeyError for an unknown column and ValueError for an unknown operator or an aggregate
    without a usable column."""
    today = today or today_local()
    rows = list(table.rows)
    described: list[str] = []
    for f in filters or []:
        if f.op not in FILTER_OPS:
            raise ValueError(f"unknown filter op {f.op!r}; use one of {', '.join(FILTER_OPS)}")
        col = table.column(f.column)
        idx = table.index_of(col)
        value = parse_filter_value(f.value, today=today)
        if f.op not in ("is_empty", "not_empty") and value.kind == "none":
            raise ValueError(f"filter on {col.header!r} with op {f.op!r} needs a value")
        rows = [r for r in rows if cell_matches(r.cells[idx], f.op, value)]
        described.append(f"{col.header} {f.op}" + ("" if f.op in ("is_empty", "not_empty") else f" {value.describe()}"))
    sort_text: str | None = None
    if sort_by:
        col = table.column(sort_by)
        idx = table.index_of(col)
        filled = sorted((r for r in rows if not r.cells[idx].empty), key=lambda r: _sort_key(r.cells[idx]), reverse=descending)
        rows = filled + [r for r in rows if r.cells[idx].empty]  # empty cells last in both directions
        sort_text = f"{col.header} {'descending' if descending else 'ascending'}"
    agg_text: str | None = None
    agg_value: float | None = None
    if aggregate:
        if aggregate not in AGGREGATE_OPS:
            raise ValueError(f"unknown aggregate {aggregate!r}; use one of {', '.join(AGGREGATE_OPS)}")
        if aggregate == "count" and not aggregate_column:
            agg_value = float(len(rows))
            agg_text = f"count of matched rows = {len(rows)}"
        else:
            if not aggregate_column:
                raise ValueError(f"aggregate {aggregate!r} needs a column")
            col = table.column(aggregate_column)
            idx = table.index_of(col)
            numbers = [r.cells[idx].number for r in rows if r.cells[idx].number is not None]
            skipped = len(rows) - len(numbers)
            if aggregate == "count":
                agg_value = float(len(numbers))
                agg_text = f"count of {col.header} over {len(rows)} matched row(s) = {len(numbers)} numeric cell(s)"
            elif not numbers:
                agg_text = f"{aggregate} of {col.header} over {len(rows)} matched row(s): no numeric cells"
            else:
                agg_value = {
                    "sum": sum(numbers),
                    "avg": sum(numbers) / len(numbers),
                    "min": min(numbers),
                    "max": max(numbers),
                }[aggregate]
                agg_text = (
                    f"{aggregate} of {col.header} over {len(rows)} matched row(s) = {format_number(agg_value)} "
                    f"({len(numbers)} numeric cell(s), {skipped} empty or non-numeric skipped)"
                )
    return QueryResult(rows, described, sort_text, agg_text, agg_value, today)


def table_from_values(values: list[list[Any]], **kw) -> Table:
    """Convenience for callers that only have formatted strings (tests, fixtures)."""
    return build_table(grid_from_values(values), **kw)


__all__ = [
    "AGGREGATE_OPS",
    "Cell",
    "Column",
    "FILTER_OPS",
    "Filter",
    "FilterValue",
    "QueryResult",
    "RawCell",
    "Row",
    "Table",
    "build_table",
    "cell_matches",
    "column_letter",
    "columns_line",
    "fold",
    "format_number",
    "grid_from_api",
    "grid_from_values",
    "make_cell",
    "parse_date",
    "parse_filter_value",
    "parse_number",
    "query",
    "render_rows",
    "row_line",
    "serial_to_date",
    "table_from_values",
    "today_local",
]
