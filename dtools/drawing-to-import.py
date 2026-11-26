#!/usr/bin/env python3
"""
drawing-to-import.py — Turn a Vectorworks equipment list into a D-Tools import file.

The drawing is usually ahead of the proposal: devices get added in
Vectorworks during design and then somebody retypes them into D-Tools.
This rolls a drawing's device list (one row per device) up into one
line per part per location, with quantities, as a CSV ready for the
D-Tools SI product import — where you map its columns once and reuse
the mapping.

Usage:
    python drawing-to-import.py "Courthouse - Device.csv" > import.csv
    python drawing-to-import.py devices.xlsx --out import.csv --phase Trim
    python drawing-to-import.py devices.csv --only-missing-from current-bom.xlsx
    python drawing-to-import.py devices.csv --no-location          # project-wide quantities

Input is a Vectorworks worksheet export, a ConnectCAD device report, or
the CSV from vectorworks/vw-export-records.py. Manufacturer ("Make"),
model and room/layer columns are matched by name; --map handles the rest.

--only-missing-from BOM writes only what the drawing has beyond an
existing BOM export — the delta to add, not the whole list — comparing
per location unless --no-location is given.

Rows with no model, or a placeholder (TBD, MISC), are left out and
listed on stderr so they don't vanish silently.

Output columns: Manufacturer, Model, Description, Category, Location,
System, Phase, Quantity.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import PLACEHOLDER_MODELS, add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, norm_key, parse_map_args  # noqa: E402

COLUMNS = ["Manufacturer", "Model", "Description", "Category", "Location", "System", "Phase", "Quantity"]


def build_import(drawing_items, by_location=True, existing_items=None, phase="", system=""):
    """Return (rows, skipped). Rows are dicts keyed by COLUMNS."""
    def key(it):
        return (norm_key(it.model), norm_key(it.location) if by_location else "")

    have = {}
    for it in existing_items or []:
        have[key(it)] = have.get(key(it), 0) + it.quantity

    rows, skipped = {}, []
    for it in drawing_items:
        if norm_key(it.model) in PLACEHOLDER_MODELS:
            skipped.append(it)
            continue
        row = rows.setdefault(key(it), {
            "Manufacturer": it.manufacturer, "Model": it.model, "Description": it.description,
            "Category": it.category, "Location": it.location if by_location else "",
            "System": it.system or system, "Phase": it.phase or phase, "Quantity": 0})
        row["Quantity"] += it.quantity
        for col, value in (("Manufacturer", it.manufacturer), ("Description", it.description),
                           ("Category", it.category)):
            row[col] = row[col] or value

    out = []
    for k, row in rows.items():
        row["Quantity"] -= have.get(k, 0)
        if row["Quantity"] > 0:
            row["Quantity"] = int(row["Quantity"]) if row["Quantity"] == int(row["Quantity"]) else row["Quantity"]
            out.append(row)
    out.sort(key=lambda r: (r["Location"].lower(), r["Manufacturer"].lower(), r["Model"].lower()))
    return out, skipped


def main():
    parser = argparse.ArgumentParser(
        description="Build a D-Tools import CSV from a Vectorworks equipment list.",
        epilog="Example: python drawing-to-import.py devices.csv --only-missing-from bom.xlsx")
    parser.add_argument("drawing", help="Drawing equipment list (.csv, .tsv, .xlsx)")
    parser.add_argument("--only-missing-from", metavar="BOM",
                        help="Existing BOM export; write only what the drawing has beyond it")
    parser.add_argument("--no-location", action="store_true", help="One line per part for the whole project")
    parser.add_argument("--phase", default="", help="Phase to put on lines that have none")
    parser.add_argument("--system", default="", help="System to put on lines that have none")
    parser.add_argument("--bom-map", action="append", default=[], metavar='FIELD="Column"',
                        help="Like --map, for the --only-missing-from file")
    parser.add_argument("--out", help="Write to this file instead of stdout")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        items, table = load_bom(args.drawing, parse_map_args(args.map), args.sheet)
        existing = (load_bom(args.only_missing_from, parse_map_args(args.bom_map))[0]
                    if args.only_missing_from else None)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    by_location = not args.no_location and table.has("location")
    if not args.no_location and not table.has("location"):
        print("Note: no room/location/layer column in the drawing list — writing project-wide "
              "quantities.", file=sys.stderr)

    rows, skipped = build_import(items, by_location, existing, args.phase, args.system)
    handle = open(args.out, "w", newline="", encoding="utf-8-sig") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(handle, COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.out:
            handle.close()

    units = sum(r["Quantity"] for r in rows)
    print(f"{len(rows)} line(s), {units:g} unit(s) from {len(items)} drawing row(s)"
          + (f" → {args.out}" if args.out else ""), file=sys.stderr)
    for it in skipped:
        print(f"  skipped row {it.row}: {it.label} — no usable model number", file=sys.stderr)


if __name__ == "__main__":
    main()
