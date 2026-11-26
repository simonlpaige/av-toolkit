#!/usr/bin/env python3
"""
bom-audit.py — Lint a D-Tools BOM before the proposal goes out.

Reads an equipment list exported from D-Tools SI (or any spreadsheet
with manufacturer / model / quantity columns) and flags the mistakes
that turn into change orders and margin erosion:

  ERROR    price below cost · zero or negative quantity · blank model
  WARNING  $0 price or cost · margin under the floor · placeholder models
           (TBD, MISC) · the same part priced two different ways · one
           model under two manufacturer spellings · no location or phase
  INFO     priced items carrying no labor hours

Then prints totals — sell, cost, margin, hours — grouped however you like.

Usage:
    python bom-audit.py project-bom.csv
    python bom-audit.py project-bom.xlsx --min-margin 30 --group-by system
    python bom-audit.py project-bom.csv --json > audit.json
    python bom-audit.py project-bom.csv --map model="Part #" --map quantity="Qty Total"

Any column layout works: export the product grid or a line-item report
to Excel/CSV. Headers are matched by name ("Qty", "Unit Price",
"Location"...); use --map for anything unusual.

Exit code: 0 clean, 1 errors found (or warnings, with --strict).

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import PLACEHOLDER_MODELS, add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, money, norm_key, parse_map_args  # noqa: E402

LEVELS = ("ERROR", "WARNING", "INFO")


def audit(items, table, min_margin: float = 20.0) -> list:
    """Return a list of {level, row, item, message} findings."""
    findings = []

    def add(level, item, message):
        findings.append({"level": level, "row": item.row if item else None,
                         "item": item.label if item else "", "message": message})

    for it in items:
        if norm_key(it.model) in PLACEHOLDER_MODELS:
            add("ERROR" if not it.model else "WARNING", it,
                "blank model" if not it.model else f"placeholder model '{it.model}'")
        if not it.manufacturer:
            add("WARNING", it, "no manufacturer")
        if it.quantity <= 0:
            add("ERROR", it, f"quantity is {it.quantity:g}")
        elif it.quantity != int(it.quantity) and it.quantity < 10:
            add("WARNING", it, f"fractional quantity {it.quantity:g}")

        price, cost = it.unit_price, it.unit_cost
        if table.has("unit_price") or table.has("ext_price"):
            if not price:
                add("WARNING", it, "sell price is $0")
        if table.has("unit_cost") or table.has("ext_cost"):
            if not cost and price:
                add("WARNING", it, "cost is $0 — margin on this line is overstated")
        if price and cost:
            margin = (price - cost) / price * 100
            if price < cost:
                add("ERROR", it, f"priced below cost ({money(price)} sell vs {money(cost)} cost)")
            elif margin < min_margin:
                add("WARNING", it, f"margin {margin:.1f}% is under the {min_margin:g}% floor")

        if table.has("location") and not it.location:
            add("WARNING", it, "no location")
        if table.has("phase") and not it.phase:
            add("WARNING", it, "no phase")
        if table.has("labor_hours") and price and not it.labor_hours:
            add("INFO", it, "no labor hours")

    # Cross-line consistency
    by_part = defaultdict(list)
    by_model = defaultdict(set)
    for it in items:
        if norm_key(it.model) not in PLACEHOLDER_MODELS:
            by_part[it.key].append(it)
            if it.manufacturer:
                by_model[norm_key(it.model)].add(it.manufacturer.strip())
    for lines in by_part.values():
        for field, name in (("unit_price", "sell prices"), ("unit_cost", "costs")):
            values = sorted({round(getattr(l, field), 2) for l in lines if getattr(l, field)})
            if len(values) > 1:
                rows = ", ".join(str(l.row) for l in lines)
                add("WARNING", lines[0], f"same part has {len(values)} different {name} "
                    f"({', '.join(money(v) for v in values)}) on rows {rows}")
    for model, makers in by_model.items():
        if len({norm_key(m) for m in makers}) > 1:
            first = next(it for it in items if norm_key(it.model) == model)
            add("WARNING", first, f"model '{first.model}' is listed under different manufacturers: "
                + ", ".join(sorted(makers)))

    findings.sort(key=lambda f: (LEVELS.index(f["level"]), f["row"] or 0))
    return findings


def totals(items, group_by=None) -> dict:
    """Sum quantity, sell, cost and hours — overall, or per group."""
    groups = defaultdict(lambda: {"lines": 0, "quantity": 0.0, "price": 0.0, "cost": 0.0, "hours": 0.0})
    for it in items:
        g = groups[(getattr(it, group_by) or "(none)") if group_by else "TOTAL"]
        g["lines"] += 1
        g["quantity"] += it.quantity
        g["price"] += it.ext_price
        g["cost"] += it.ext_cost
        g["hours"] += (it.labor_hours or 0.0)
    for g in groups.values():
        g["margin_pct"] = round((g["price"] - g["cost"]) / g["price"] * 100, 1) if g["price"] else 0.0
        for k in ("price", "cost", "hours", "quantity"):
            g[k] = round(g[k], 2)
    return dict(groups)


def print_totals(title, groups):
    print(f"\n{title}")
    print(f"  {'':<32}{'Lines':>6}{'Qty':>8}{'Sell':>15}{'Cost':>15}{'Margin':>8}{'Hours':>9}")
    for name in sorted(groups, key=lambda n: -groups[n]["price"]):
        g = groups[name]
        print(f"  {name[:31]:<32}{g['lines']:>6}{g['quantity']:>8g}{money(g['price']):>15}"
              f"{money(g['cost']):>15}{g['margin_pct']:>7.1f}%{g['hours']:>9g}")


def main():
    parser = argparse.ArgumentParser(
        description="Audit a D-Tools BOM export for pricing and data problems.",
        epilog="Example: python bom-audit.py project-bom.csv --min-margin 30 --group-by location")
    parser.add_argument("bom", help="BOM export (.csv, .tsv, .xlsx)")
    parser.add_argument("--min-margin", type=float, default=20.0,
                        help="Warn below this margin %% (default 20)")
    parser.add_argument("--group-by", choices=["location", "system", "phase", "manufacturer", "category"],
                        help="Also total by this field")
    parser.add_argument("--hide", action="append", default=[], choices=["info", "warning"],
                        help="Hide a level of findings (repeatable)")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on warnings too")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        items, table = load_bom(args.bom, parse_map_args(args.map), args.sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if not items:
        print(f"Error: no equipment rows found in {args.bom}", file=sys.stderr)
        sys.exit(2)

    findings = [f for f in audit(items, table, args.min_margin)
                if f["level"].lower() not in args.hide]
    counts = {lvl: sum(f["level"] == lvl for f in findings) for lvl in LEVELS}
    overall = totals(items)
    grouped = totals(items, args.group_by) if args.group_by else None

    if args.json:
        print(json.dumps({"file": str(args.bom), "columns": table.columns, "counts": counts,
                          "findings": findings, "totals": overall["TOTAL"],
                          "groups": grouped}, indent=2))
    else:
        print(f"BOM AUDIT — {table.path.name}")
        print(f"  {len(items)} line items · columns read: {', '.join(table.columns)}")
        if table.unmapped:
            print(f"  columns ignored: {', '.join(table.unmapped)}")
        for level in LEVELS:
            hits = [f for f in findings if f["level"] == level]
            if hits:
                print(f"\n{level} ({len(hits)})")
                for f in hits:
                    print(f"  row {f['row']:<5} {f['item'][:40]:<41} {f['message']}")
        if not findings:
            print("\n✓ No problems found.")
        print_totals("TOTALS", overall)
        if grouped:
            print_totals(f"BY {args.group_by.upper()}", grouped)

    sys.exit(1 if counts["ERROR"] or (args.strict and counts["WARNING"]) else 0)


if __name__ == "__main__":
    main()
