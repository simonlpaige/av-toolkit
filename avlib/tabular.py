"""
tabular.py — Read the spreadsheets that design tools export.

D-Tools SI, Vectorworks worksheets and ConnectCAD reports all leave as
CSV, tab-delimited text or .xlsx, each with its own column names, title
rows above the header, and its own idea of text encoding. This module
turns any of them into a list of dicts keyed by *canonical* field names,
so the tools built on top never care which program made the file.

No third-party dependencies: .xlsx is read with zipfile + ElementTree.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


class TableError(ValueError):
    """Raised when a file cannot be read as a table."""


def norm_header(text) -> str:
    """Normalise a column header for matching: 'Ext. Price ($)' -> 'ext price'."""
    text = re.sub(r"\([^)]*\)", " ", str(text or "").lower())
    text = text.replace("#", " no ").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# ---------------------------------------------------------------- raw readers

def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    # Vectorworks on Windows can write UTF-16 without a BOM.
    if len(raw) > 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _read_delimited(path: Path) -> list[list[str]]:
    text = _decode(path.read_bytes())
    sample = text[:8192]
    counts = {d: sample.count(d) for d in (",", "\t", ";")}
    delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
    if path.suffix.lower() == ".tsv":
        delimiter = "\t"
    return [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _read_xlsx(path: Path, sheet: str | None) -> list[list[str]]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise TableError(f"{path.name}: not a valid .xlsx file ({e})") from e
    with zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))

        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall(f"{_PKG_REL}Relationship")}
        sheets = [(s.get("name"), targets.get(s.get(f"{_REL_NS}id")))
                  for s in wb.findall("m:sheets/m:sheet", _NS)]
        if not sheets:
            raise TableError(f"{path.name}: workbook has no sheets")
        if sheet is None:
            name, target = sheets[0]
        else:
            match = [s for s in sheets if s[0].lower() == sheet.lower()]
            if not match:
                names = ", ".join(s[0] for s in sheets)
                raise TableError(f"{path.name}: no sheet named '{sheet}' (have: {names})")
            name, target = match[0]
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target

        rows: list[list[str]] = []
        root = ET.fromstring(zf.read(target))
        for row in root.iter(f"{{{_NS['m']}}}row"):
            cells: list[str] = []
            for c in row.findall("m:c", _NS):
                idx = _col_index(c.get("r", "")) if c.get("r") else len(cells)
                while len(cells) <= idx:
                    cells.append("")
                kind = c.get("t")
                v = c.find("m:v", _NS)
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                elif v is None or v.text is None:
                    value = ""
                elif kind == "s":
                    value = shared[int(v.text)]
                elif kind == "b":
                    value = "TRUE" if v.text == "1" else "FALSE"
                else:
                    value = v.text
                    # 12.0 -> 12, 3.3000000000000003 -> 3.3
                    if kind in (None, "n") and re.fullmatch(r"-?\d+\.\d+(E-?\d+)?", value):
                        value = repr(round(float(value), 10))
                        if value.endswith(".0"):
                            value = value[:-2]
                cells[idx] = value
            rows.append(cells)
        return rows


def read_rows(path, sheet: str | None = None) -> list[list[str]]:
    """Read a .csv/.tsv/.txt/.xlsx file into a list of rows of strings."""
    path = Path(path)
    if not path.exists():
        raise TableError(f"{path}: file not found")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        rows = _read_xlsx(path, sheet)
    elif suffix == ".xls":
        raise TableError(f"{path.name}: legacy .xls is not supported — re-save as .xlsx or .csv")
    else:
        rows = _read_delimited(path)
    return [[str(c).strip() for c in row] for row in rows]


# ------------------------------------------------------------ header mapping

class Table:
    """Rows of a spreadsheet keyed by canonical field name."""

    def __init__(self, path, rows, columns, unmapped, header_row):
        self.path = Path(path)
        self.rows: list[dict] = rows          # each has "_row" = 1-based file row
        self.columns: dict[str, str] = columns  # canonical field -> header text in file
        self.unmapped: list[str] = unmapped   # headers that matched nothing
        self.header_row: int = header_row     # 1-based

    def has(self, field: str) -> bool:
        return field in self.columns

    def describe(self) -> str:
        cols = ", ".join(f"{f}<-'{h}'" for f, h in self.columns.items())
        return f"{self.path.name}: {len(self.rows)} rows, header on row {self.header_row}; {cols}"


def _match_headers(headers: list[str], aliases: dict[str, list[str]],
                   overrides: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    normed = [norm_header(h) for h in headers]
    mapping: dict[str, int] = {}
    used: set[int] = set()

    for field, header in overrides.items():
        want = norm_header(header)
        for i, n in enumerate(normed):
            if n == want and i not in used:
                mapping[field] = i
                used.add(i)
                break

    # Exact alias matches first, in alias priority order, so "Model" wins
    # over "Model Description" regardless of column order.
    for field, names in aliases.items():
        if field in mapping:
            continue
        for alias in [field.replace("_", " ")] + list(names):
            want = norm_header(alias)
            hit = next((i for i, n in enumerate(normed) if n == want and i not in used), None)
            if hit is not None:
                mapping[field] = hit
                used.add(hit)
                break

    unmapped = [h for i, h in enumerate(headers) if i not in used and h.strip()]
    return mapping, unmapped


def parse_map_args(pairs) -> dict[str, str]:
    """Turn ['model=Part No', ...] from --map into a dict."""
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise TableError(f"--map expects field=Column Header, got '{pair}'")
        field, header = pair.split("=", 1)
        out[field.strip().lower().replace(" ", "_")] = header.strip()
    return out


def load_table(path, aliases: dict[str, list[str]], *, required=(), overrides=None,
               sheet: str | None = None, keep_blank=False) -> Table:
    """
    Load a spreadsheet and map its columns onto canonical field names.

    aliases   canonical_field -> list of header spellings to accept
    required  each entry is a field, or a tuple of fields of which one must exist
    overrides field -> exact header text, from the user's --map arguments

    Title rows above the real header are skipped automatically: the header
    is the row (within the first 30) that matches the most known fields.
    """
    overrides = overrides or {}
    unknown = [f for f in overrides if f not in aliases]
    if unknown:
        raise TableError(f"--map: unknown field(s) {', '.join(unknown)}. "
                         f"Known fields: {', '.join(aliases)}")
    raw = read_rows(path, sheet)
    if not raw:
        raise TableError(f"{Path(path).name}: file is empty")

    best = (0, 0, {}, [])
    for i, row in enumerate(raw[:30]):
        mapping, unmapped = _match_headers(row, aliases, overrides)
        if len(mapping) > best[0]:
            best = (len(mapping), i, mapping, unmapped)
    score, header_idx, mapping, unmapped = best
    headers = raw[header_idx]

    missing_override = [f"{f}='{h}'" for f, h in overrides.items() if f not in mapping]
    if missing_override:
        raise TableError(f"{Path(path).name}: --map column not found: {', '.join(missing_override)}. "
                         f"Columns in file: {', '.join(h for h in headers if h)}")

    missing = []
    for req in required:
        group = (req,) if isinstance(req, str) else tuple(req)
        if not any(f in mapping for f in group):
            missing.append(" or ".join(group))
    if missing:
        raise TableError(
            f"{Path(path).name}: could not find a column for: {', '.join(missing)}.\n"
            f"  Columns in file (row {header_idx + 1}): {', '.join(h for h in headers if h) or '(none)'}\n"
            f"  Point me at the right one with --map, e.g. --map {missing[0].split(' or ')[0]}=\"Your Column\"")

    rows = []
    for n, row in enumerate(raw[header_idx + 1:], start=header_idx + 2):
        record = {f: (row[i] if i < len(row) else "") for f, i in mapping.items()}
        if not keep_blank and not any(record.values()):
            continue
        record["_row"] = n
        rows.append(record)

    return Table(path, rows, {f: headers[i] for f, i in mapping.items()}, unmapped, header_idx + 1)


# ------------------------------------------------------------- value parsing

def to_number(value, default: float | None = 0.0) -> float | None:
    """'$1,234.50' -> 1234.5, '(120.00)' -> -120.0, '2 RU' -> 2.0, '' -> default."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return default
    negative = text.startswith("(") and text.endswith(")")
    m = re.search(r"-?\d[\d,]*\.?\d*|-?\.\d+", text)
    if not m:
        return default
    number = float(m.group().replace(",", ""))
    return -abs(number) if negative else number


FT_PER_M = 3.28084


def to_feet(value, default_unit: str = "ft") -> float | None:
    """
    Parse a length into feet. Understands 25, 25ft, 25', 25'-6", 25' 6",
    7.6m, 7600mm, 300 in. A bare number uses default_unit ('ft' or 'm').
    Returns None for blank or unparseable input.
    """
    if isinstance(value, (int, float)):
        return float(value) * (FT_PER_M if default_unit == "m" else 1.0)
    text = str(value or "").strip().lower().replace("’", "'").replace("”", '"').replace("″", '"')
    if not text:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:'|ft|feet)\s*-?\s*(\d+(?:\.\d+)?)?\s*(?:\"|in|inches)?", text)
    if m and ("'" in text or "f" in text):
        return float(m.group(1)) + float(m.group(2) or 0) / 12.0
    m = re.fullmatch(r"(-?\d[\d,]*\.?\d*)\s*([a-z\"]*)\.?", text)
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    unit = m.group(2) or default_unit
    factors = {"ft": 1.0, "feet": 1.0, "foot": 1.0, "m": FT_PER_M, "meter": FT_PER_M,
               "meters": FT_PER_M, "metre": FT_PER_M, "metres": FT_PER_M,
               "mm": FT_PER_M / 1000, "cm": FT_PER_M / 100,
               "in": 1 / 12, '"': 1 / 12, "inch": 1 / 12, "inches": 1 / 12}
    if unit not in factors:
        return None
    return number * factors[unit]


def norm_key(text) -> str:
    """Collapse a part number / name for matching: 'DMP 128 Plus C AT' -> 'DMP128PLUSCAT'."""
    return re.sub(r"[^A-Z0-9]+", "", str(text or "").upper())


def money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"
