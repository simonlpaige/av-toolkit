#!/usr/bin/env python3
"""
bom-diff.py — What changed between two revisions of a BOM?

Compare two equipment-list exports (D-Tools SI or any spreadsheet) and
get the list you need for a change order: parts added, parts removed,
quantities changed, prices and costs changed — with the dollar impact.

Usage:
    python bom-diff.py rev-A.csv rev-B.csv
    python bom-diff.py rev-A.xlsx rev-B.xlsx --by-location
    python bom-diff.py rev-A.csv rev-B.csv --format markdown > change-order.md
    python bom-diff.py rev-A.csv rev-B.csv --format csv > changes.csv

Parts are matched on manufacturer + model, ignoring case, spacing and
punctuation ("DMP 128 Plus" = "dmp128plus"). With --by-location a part
moved from one room to another shows as removed there and added here.

Exit code: 0 no changes, 1 changes found, 2 could not read a file.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, money, norm_key, parse_map_args  # noqa: E402


def rollup(items, by_location=False) -> dict:
    """Collapse line items into one record per part (per location, optionally)."""
    parts = {}
    for it in items:
        key = it.key + ((norm_key(it.location),) if by_location else ())
        p = parts.setdefault(key, {"manufacturer": it.manufacturer, "model": it.model,
                                   "location": it.location if by_location else "",
                                   "quantity": 0.0, "price": 0.0, "cost": 0.0})
        p["quantity"] += it.quantity
        p["price"] += it.ext_price
        p["cost"] += it.ext_cost
    for p in parts.values():
        q = p["quantity"] or 1
        p["unit_price"], p["unit_cost"] = round(p["price"] / q, 2), round(p["cost"] / q, 2)
    return parts


def diff_boms(old_items, new_items, by_location=False) -> list:
    """Return change records, sorted by change type then dollar impact."""
    old, new = rollup(old_items, by_location), rollup(new_items, by_location)
    changes = []
    for key in set(old) | set(new):
        a, b = old.get(key), new.get(key)
        ref = b or a
        rec = {"manufacturer": ref["manufacturer"], "model": ref["model"], "location": ref["location"],
               "old_qty": a["quantity"] if a else 0, "new_qty": b["quantity"] if b else 0,
               "old_unit_price": a["unit_price"] if a else None,
               "new_unit_price": b["unit_price"] if b else None,
               "old_unit_cost": a["unit_cost"] if a else None,
               "new_unit_cost": b["unit_cost"] if b else None,
               "price_delta": round((b["price"] if b else 0) - (a["price"] if a else 0), 2),
               "cost_delta": round((b["cost"] if b else 0) - (a["cost"] if a else 0), 2)}
        if a is None:
            rec["change"] = "added"
        elif b is None:
            rec["change"] = "removed"
        else:
            kinds = []
            if a["quantity"] != b["quantity"]:
                kinds.append("quantity")
            if a["unit_price"] != b["unit_price"]:
                kinds.append("price")
            if a["unit_cost"] != b["unit_cost"]:
                kinds.append("cost")
            if not kinds:
                continue
            rec["change"] = "+".join(kinds)
        changes.append(rec)
    order = {"added": 0, "removed": 1}
    changes.sort(key=lambda c: (order.get(c["change"], 2), -abs(c["price_delta"]),
                                c["manufacturer"], c["model"], c["location"]))
    return changes


def describe(c) -> str:
    bits = []
    if c["change"] == "added":
        bits.append(f"qty {c['new_qty']:g} @ {money(c['new_unit_price'])}")
    elif c["change"] == "removed":
        bits.append(f"qty {c['old_qty']:g} @ {money(c['old_unit_price'])}")
    else:
        if "quantity" in c["change"]:
            bits.append(f"qty {c['old_qty']:g} → {c['new_qty']:g}")
        if "price" in c["change"]:
            bits.append(f"price {money(c['old_unit_price'])} → {money(c['new_unit_price'])}")
        if "cost" in c["change"]:
            bits.append(f"cost {money(c['old_unit_cost'])} → {money(c['new_unit_cost'])}")
    return ", ".join(bits)


def render(changes, old_name, new_name, fmt, by_location) -> str:
    net_price = sum(c["price_delta"] for c in changes)
    net_cost = sum(c["cost_delta"] for c in changes)
    if fmt == "json":
        return json.dumps({"old": old_name, "new": new_name, "net_price_delta": round(net_price, 2),
                           "net_cost_delta": round(net_cost, 2), "changes": changes}, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["change", "manufacturer", "model", "location", "old_qty", "new_qty", "old_unit_price",
                "new_unit_price", "old_unit_cost", "new_unit_cost", "price_delta", "cost_delta"]
        w = csv.DictWriter(buf, cols, lineterminator="\n")
        w.writeheader()
        w.writerows(changes)
        return buf.getvalue().rstrip("\n")

    def name(c):
        part = " ".join(p for p in (c["manufacturer"], c["model"]) if p)
        return part + (f" @ {c['location'] or '(no location)'}" if by_location else "")

    lines = []
    if fmt == "markdown":
        lines += [f"# BOM changes: {old_name} → {new_name}", "",
                  "| Change | Item | Detail | Sell Δ | Cost Δ |", "|---|---|---|---:|---:|"]
        for c in changes:
            cells = [c["change"], name(c), describe(c), money(c["price_delta"]), money(c["cost_delta"])]
            lines.append("| " + " | ".join(x.replace("|", "\\|") for x in cells) + " |")
        lines += ["", f"**Net sell change: {money(net_price)}** · net cost change: {money(net_cost)}"]
        return "\n".join(lines)

    lines.append(f"BOM CHANGES  {old_name}  →  {new_name}")
    if not changes:
        lines.append("\n✓ No differences.")
        return "\n".join(lines)
    titles = {"added": "ADDED", "removed": "REMOVED"}
    current = None
    for c in changes:
        section = titles.get(c["change"], "CHANGED")
        if section != current:
            current = section
            lines.append(f"\n{section}")
        lines.append(f"  {name(c)[:48]:<49} {describe(c):<44} {money(c['price_delta']):>13}")
    lines.append(f"\n  {len(changes)} change(s) · net sell {money(net_price)} · net cost {money(net_cost)}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Compare two BOM revisions and price the difference.",
        epilog="Example: python bom-diff.py rev-A.csv rev-B.csv --by-location")
    parser.add_argument("old", help="Earlier BOM export (.csv, .tsv, .xlsx)")
    parser.add_argument("new", help="Later BOM export")
    parser.add_argument("--by-location", action="store_true", help="Track each part per location")
    parser.add_argument("--format", choices=["text", "markdown", "csv", "json"], default="text")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        overrides = parse_map_args(args.map)
        old_items, _ = load_bom(args.old, overrides, args.sheet)
        new_items, _ = load_bom(args.new, overrides, args.sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    changes = diff_boms(old_items, new_items, args.by_location)
    print(render(changes, Path(args.old).name, Path(args.new).name, args.format, args.by_location))
    sys.exit(1 if changes else 0)


if __name__ == "__main__":
    main()
