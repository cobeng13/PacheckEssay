import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable


APP_VERSION = "1.0.0"


def clean(value: str) -> str:
    s = (value or "").strip()
    return "" if s.lower() == "nan" else s


def find_col(header: list[str], candidates: list[str]) -> str | None:
    lower = [h.strip().lower() for h in header]
    for cand in candidates:
        if cand in lower:
            return header[lower.index(cand)]
    return None


def detect_name_columns(header: list[str]) -> tuple[str | None, str | None, str | None]:
    trimmed = [h.strip() for h in header]
    first_col = find_col(trimmed, ["first name", "firstname", "given name", "first", "givenname", "given"])
    last_col = find_col(trimmed, ["last name", "lastname", "surname", "last", "family name", "familyname"])
    name_col = find_col(trimmed, ["name", "full name", "fullname"])
    return first_col, last_col, name_col


def detect_id_column(header: list[str]) -> str | None:
    trimmed = [h.strip() for h in header]
    return find_col(
        trimmed,
        ["student id", "id", "sis user id", "sis id", "user id", "user_id", "student_number", "student number"],
    )


def detect_response_columns(header: list[str]) -> list[str]:
    trimmed = [h.strip() for h in header]
    return [h for h in trimmed if h.lower().startswith("response")]


def compose_display_name(
    row: dict[str, str], first_col: str | None, last_col: str | None, name_col: str | None
) -> str:
    first = clean(row.get(first_col, "")) if first_col else ""
    last = clean(row.get(last_col, "")) if last_col else ""
    single = clean(row.get(name_col, "")) if name_col else ""
    if last or first:
        if last and first:
            return f"{last}, {first}"
        return last or first
    return single if single else "(No name)"


def normalize_key(text: str) -> str:
    t = clean(text).lower()
    t = re.sub(r"\s+", "_", t)
    t = re.sub(r"[^a-z0-9_]+", "", t)
    return t.strip("_")


def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    name = name.replace(",", "")
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    return name or "NoName"


def ensure_unique_stems(stems: Iterable[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for stem in stems:
        base = sanitize_filename(stem)
        n = seen.get(base, 0)
        if n == 0:
            out.append(base)
            seen[base] = 1
            continue
        while True:
            n += 1
            candidate = f"{base}_{n}"
            if candidate not in seen:
                out.append(candidate)
                seen[base] = n
                seen[candidate] = 1
                break
    return out


def now_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def app_dir() -> Path:
    return Path(os.path.abspath(os.path.dirname(__file__)))

