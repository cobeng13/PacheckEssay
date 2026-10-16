import csv
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .utils import (
    clean,
    compose_display_name,
    detect_id_column,
    detect_name_columns,
    detect_response_columns,
    ensure_unique_stems,
    normalize_key,
    sanitize_filename,
)


@dataclass
class CsvPreview:
    headers: list[str]
    rows: list[dict[str, str]]
    first_col: str | None
    last_col: str | None
    name_col: str | None
    id_col: str | None
    response_cols: list[str]


@dataclass
class StudentRecord:
    row_index: int
    student_id: str
    student_id_raw: str
    display_name: str
    safe_stem: str
    responses_by_column: dict[str, str]
    raw_row: dict[str, str]


class _SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell_parts: list[str] = []
        self._cell_has_content = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag = tag.lower()
        if tag == "table":
            if not self._in_table:
                self._in_table = True
                self._current_table = []
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._in_row = True
            self._current_row = []
            return
        if tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._cell_parts = []
            self._cell_has_content = False
            return
        if self._in_cell and tag in {"br", "p", "div", "li"}:
            if self._cell_has_content:
                self._cell_parts.append("\n")
            if tag == "li":
                self._cell_parts.append("- ")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            text = "".join(self._cell_parts).strip()
            self._current_row.append(text)
            self._in_cell = False
            self._cell_parts = []
            self._cell_has_content = False
            return
        if tag == "tr" and self._in_row:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = []
            self._in_row = False
            return
        if tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = []
            self._in_table = False

    def handle_data(self, data: str):
        if not self._in_cell or not data:
            return
        self._cell_parts.append(data)
        if data.strip():
            self._cell_has_content = True


def _read_text_with_fallbacks(path: Path) -> str:
    encodings = ["utf-8-sig", "utf-8", "latin-1"]
    last_error: Exception | None = None
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Could not read text file: {last_error}")


def _normalize_json_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], list):
        data = data[0]
    if not isinstance(data, list):
        raise ValueError("JSON must contain a list of row objects.")

    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"JSON row {idx} is not an object.")
        rows.append(item)
    return rows


def _rows_to_headers_and_strings(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    headers: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            key_text = str(key).strip()
            if not key_text or key_text in seen:
                continue
            seen.add(key_text)
            headers.append(key_text)

    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized: dict[str, str] = {}
        for header in headers:
            value = row.get(header, "")
            if value is None:
                normalized[header] = ""
            elif isinstance(value, str):
                normalized[header] = value
            else:
                normalized[header] = str(value)
        normalized_rows.append(normalized)
    return headers, normalized_rows


def _read_json_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        data = json.loads(_read_text_with_fallbacks(path))
        rows = _normalize_json_rows(data)
        return _rows_to_headers_and_strings(rows)
    except Exception as e:
        raise RuntimeError(f"Could not read JSON: {e}") from e


def _read_html_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    parser = _SimpleTableParser()
    parser.feed(_read_text_with_fallbacks(path))
    if not parser.tables:
        raise RuntimeError("Could not find any HTML tables.")

    table = next((table for table in parser.tables if len(table) >= 2), parser.tables[0])
    if not table:
        raise RuntimeError("HTML table is empty.")

    headers = [cell.strip() for cell in table[0]]
    if not any(headers):
        raise RuntimeError("HTML table is missing header cells.")

    rows: list[dict[str, str]] = []
    for raw_row in table[1:]:
        if not raw_row:
            continue
        padded = list(raw_row[: len(headers)])
        if len(padded) < len(headers):
            padded.extend([""] * (len(headers) - len(padded)))
        rows.append({headers[idx]: padded[idx] for idx in range(len(headers))})

    return headers, rows


def _read_tabular_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _read_json_rows(path)
    if suffix in {".html", ".htm"}:
        return _read_html_rows(path)
    return _read_csv_rows(path)


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    encodings = ["utf-8-sig", "utf-8", "latin-1"]
    last_error: Exception | None = None
    for enc in encodings:
        try:
            with path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                rows = [dict(r) for r in reader]
                return [h.strip() for h in headers], rows
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Could not read CSV: {last_error}")


def load_preview(csv_path: str, max_rows: int = 10) -> CsvPreview:
    path = Path(csv_path)
    headers, rows = _read_tabular_rows(path)
    if not headers:
        raise ValueError("Input file has no usable columns.")
    first_col, last_col, name_col = detect_name_columns(headers)
    id_col = detect_id_column(headers)
    response_cols = detect_response_columns(headers)
    return CsvPreview(
        headers=headers,
        rows=rows[:max_rows],
        first_col=first_col,
        last_col=last_col,
        name_col=name_col,
        id_col=id_col,
        response_cols=response_cols,
    )


def _derive_student_id(
    row: dict[str, str],
    id_col: str | None,
    first_col: str | None,
    last_col: str | None,
    name_col: str | None,
) -> tuple[str, str]:
    raw_id = clean(row.get(id_col, "")) if id_col else ""
    if raw_id:
        return normalize_key(raw_id) or raw_id, raw_id
    display = compose_display_name(row, first_col, last_col, name_col)
    fallback = normalize_key(display) or f"student_{abs(hash(display)) % 1000000}"
    return fallback, ""


def build_students(
    csv_path: str,
    first_col: str | None,
    last_col: str | None,
    name_col: str | None,
    id_col: str | None,
    response_columns: list[str],
) -> tuple[list[str], list[StudentRecord]]:
    headers, rows = _read_tabular_rows(Path(csv_path))
    if not headers:
        raise ValueError("Input file has no usable columns.")
    missing = [c for c in response_columns if c not in headers]
    if missing:
        raise ValueError(f"Response column(s) not in CSV: {missing}")

    students: list[StudentRecord] = []
    tentative_stems: list[str] = []

    for idx, row in enumerate(rows, start=1):
        if any(not isinstance(k, str) or (v is not None and not isinstance(v, str)) for k, v in row.items()):
            raise ValueError('Malformed row: check the number of CSV fields.')
        row_clean: dict[str, str] = {k: clean(v) for k, v in row.items()}
        sid_norm, sid_raw = _derive_student_id(row_clean, id_col, first_col, last_col, name_col)
        display = compose_display_name(row_clean, first_col, last_col, name_col)
        stem_seed = f"{sid_norm}_{display}" if sid_raw else display
        tentative_stems.append(sanitize_filename(stem_seed))

        responses = {c: clean(row_clean.get(c, "")) for c in response_columns}
        students.append(
            StudentRecord(
                row_index=idx,
                student_id=sid_norm,
                student_id_raw=sid_raw,
                display_name=display,
                safe_stem="",
                responses_by_column=responses,
                raw_row=row_clean,
            )
        )

    unique_stems = ensure_unique_stems(tentative_stems)
    for i, stem in enumerate(unique_stems):
        students[i].safe_stem = stem

    return headers, students


def preview_rows_for_tree(preview: CsvPreview) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in preview.rows:
        out.append([row.get(h, "") for h in preview.headers])
    return out
