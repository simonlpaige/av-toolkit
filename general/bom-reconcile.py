#!/usr/bin/env python3
"""
bom-reconcile.py — Does the drawing match the BOM?

The proposal comes out of D-Tools; the drawings come out of Vectorworks.
They are edited by different people on different days, and the gap
between them is equipment that gets drawn but never ordered, or ordered
with nowhere to go. This compares the two lists and tells you:

  - Drawn but not in the BOM        (nobody priced it)
  - In the BOM but not drawn        (nowhere to install it)
  - Quantity differs                (overall, or per room with --by-location)
  - Probable naming mismatches      ("DMP 128 Plus C" vs "DMP128PlusC AT")

Usage:
    python bom-reconcile.py dtools-bom.csv vectorworks-devices.csv
    python bom-reconcile.py dtools-bom.xlsx devices.csv --by-location
    python bom-reconcile.py bom.csv devices.csv --ignore "*cable*" --ignore "*plate*"
    python bom-reconcile.py bom.csv devices.csv --json

Inputs:
    BOM      A D-Tools SI export with manufacturer, model, quantity.
    Drawing  A Vectorworks worksheet export, a ConnectCAD device report,
             or the CSV written by vectorworks/vw-export-records.py.
             One row per device is fine — a missing Quantity column
             counts each row as one. ConnectCAD's "Make" is understood
             as manufacturer.

Parts are matched on model number, ignoring case, spaces and
punctuation. Use --map / --drawing-map when a column has an odd name.
BOM lines that never appear on drawings (cable, connectors, labor)
are noise here: drop them with --ignore, which matches model, category
or description.

Exit code: 0 lists agree, 1 discrepancies, 2 could not read a file.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import difflib
import fnmatch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import PLACEHOLDER_MODELS, add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, norm_key, parse_map_args  # noqa: E402


def _ignored(item, patterns) -> bool:
    fields = [item.model, item.category, item.description, item.manufacturer]
    return any(fnmatch.fnmatch(f.lower(), p.lower()) for p in patterns for f in fields if f)


def count_parts(items, by_location=False, ignore=()) -> dict:
    """model key (+location key) -> {label, manufacturer, location, quantity}"""
    parts = {}
    for it in items:
        model_key = norm_key(it.model)
        if model_key in PLACEHOLDER_MODELS or _ignored(it, ignore):
            continue
        key = (model_key, norm_key(it.location) if by_location else "")
        p = parts.setdefault(key, {"label": it.label, "model": it.model, "manufacturer": it.manufacturer,
                                   "location": it.location if by_location else "", "quantity": 0.0})
        p["quantity"] += it.quantity
    return parts


def reconcile(bom_items, drawing_items, by_location=False, ignore=()) -> dict:
    """Compare the two lists. Returns the four discrepancy lists."""
    bom = count_parts(bom_items, by_location, ignore)
    drawn = count_parts(drawing_items, by_location, ignore)
    result = {"not_in_bom": [], "not_on_drawing": [], "quantity": [], "manufacturer": [],
              "matched": 0}

    for key in sorted(set(bom) | set(drawn)):
        b, d = bom.get(key), drawn.get(key)
        if b and d:
            if b["quantity"] != d["quantity"]:
                result["quantity"].append({"item": b["label"], "location": b["location"],
                                           "bom": b["quantity"], "drawing": d["quantity"]})
            else:
                result["matched"] += 1
            if (b["manufacturer"] and d["manufacturer"]
                    and norm_key(b["manufacturer"]) != norm_key(d["manufacturer"])):
                result["manufacturer"].append({"item": b["model"], "bom": b["manufacturer"],
                                               "drawing": d["manufacturer"]})
        elif d:
            result["not_in_bom"].append({"item": d["label"], "location": d["location"],
                                         "drawing": d["quantity"], "_key": key})
        else:
            result["not_on_drawing"].append({"item": b["label"], "location": b["location"],
                                             "bom": b["quantity"], "_key": key})

    # A part "missing" from both sides under nearly the same name is one typo, not two problems.
    bom_only = {r["_key"][0]: r for r in result["not_on_drawing"]}
    for r in result["not_in_bom"]:
        near = difflib.get_close_matches(r["_key"][0], list(bom_only), n=1, cutoff=0.75)
        if near:
            r["possible_match"] = bom_only[near[0]]["item"]
            bom_only[near[0]].setdefault("possible_match", r["item"])
    seen = set()
    result["manufacturer"] = [m for m in result["manufacturer"]
                              if (k := (m["item"], m["bom"], m["drawing"])) not in seen and not seen.add(k)]
    for rows in (result["not_in_bom"], result["not_on_drawing"]):
        for r in rows:
            del r["_key"]
    return result


def print_report(result, bom_name, drawing_name, by_location):
    def where(r):
        return f"  [{r['location'] or 'no location'}]" if by_location else ""

    print(f"BOM vs DRAWING — {bom_name}  ⇄  {drawing_name}")
    sections = [
        ("DRAWN BUT NOT IN THE BOM", result["not_in_bom"],
         lambda r: f"{r['item'][:44]:<45} drawn ×{r['drawing']:g}{where(r)}"),
        ("IN THE BOM BUT NOT DRAWN", result["not_on_drawing"],
         lambda r: f"{r['item'][:44]:<45} BOM ×{r['bom']:g}{where(r)}"),
        ("QUANTITY DIFFERS", result["quantity"],
         lambda r: f"{r['item'][:44]:<45} BOM ×{r['bom']:g}, drawn ×{r['drawing']:g}{where(r)}"),
        ("MANUFACTURER SPELLED DIFFERENTLY", result["manufacturer"],
         lambda r: f"{r['item'][:44]:<45} BOM '{r['bom']}', drawing '{r['drawing']}'"),
    ]
    for title, rows, fmt in sections:
        if rows:
            print(f"\n{title} ({len(rows)})")
            for r in rows:
                print("  " + fmt(r))
                if r.get("possible_match"):
                    print(f"      ↳ probably the same part as: {r['possible_match']}")
    problems = sum(len(rows) for _, rows, _ in sections[:3])
    print(f"\n  {result['matched']} part(s) agree · {problems} discrepancy(ies)")
    if not problems:
        print("  ✓ Drawing and BOM match.")


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile a D-Tools BOM against a Vectorworks equipment list.",
        epilog="Example: python bom-reconcile.py bom.csv devices.csv --by-location")
    parser.add_argument("bom", help="BOM export (.csv, .tsv, .xlsx)")
    parser.add_argument("drawing", help="Drawing equipment list (.csv, .tsv, .xlsx)")
    parser.add_argument("--by-location", action="store_true", help="Compare quantities per location")
    parser.add_argument("--ignore", action="append", default=[], metavar="PATTERN",
                        help="Skip items whose model/category/description matches (wildcards ok)")
    parser.add_argument("--drawing-map", action="append", default=[], metavar='FIELD="Column"',
                        help="Like --map, for the drawing file")
    parser.add_argument("--drawing-sheet", help="Worksheet name in the drawing .xlsx")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        bom_items, _ = load_bom(args.bom, parse_map_args(args.map), args.sheet)
        drawing_items, drawing_table = load_bom(args.drawing, parse_map_args(args.drawing_map),
                                                args.drawing_sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if args.by_location and not drawing_table.has("location"):
        print("Error: --by-location needs a location/room/layer column in the drawing list "
              "(use --drawing-map location=\"...\")", file=sys.stderr)
        sys.exit(2)

    result = reconcile(bom_items, drawing_items, args.by_location, args.ignore)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result, Path(args.bom).name, Path(args.drawing).name, args.by_location)
    sys.exit(1 if result["not_in_bom"] or result["not_on_drawing"] or result["quantity"] else 0)


if __name__ == "__main__":
    main()
