"""CSV export (lib/export/csv.ts): RFC 4180, a UTF-8 BOM for Excel, and no formula injection."""

import re
from collections.abc import Callable, Iterable
from typing import Any

_FORMULA_START = re.compile(r"^[=+\-@\t\r]")
BOM = "\ufeff"


def _cell(value: Any) -> str:
    # Numbers are written as-is (so "-500" stays a number); text that looks like a formula is
    # prefixed with an apostrophe so a description such as "=HYPERLINK(...)" never runs.
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int | float):
        text = str(int(value)) if float(value).is_integer() else str(value)
    else:
        text = "" if value is None else str(value)
        if _FORMULA_START.match(text):
            text = f"'{text}"
    return f'"{text.replace(chr(34), chr(34) * 2)}"' if re.search(r'[",\r\n]', text) else text


def to_csv[T](rows: Iterable[T], columns: list[tuple[str, Callable[[T], Any]]]) -> str:
    lines = [",".join(_cell(header) for header, _ in columns)]
    lines += [",".join(_cell(value(row)) for _, value in columns) for row in rows]
    return BOM + "\r\n".join(lines)


def rows_to_csv(rows: list[list[Any]]) -> str:
    """Rows without a header line (the ready-made reports' layout)."""
    width = max((len(row) for row in rows), default=0)
    return BOM + "\r\n".join(
        ",".join(_cell(row[i] if i < len(row) else "") for i in range(width)) for row in rows
    )
