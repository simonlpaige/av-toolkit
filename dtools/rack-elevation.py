#!/usr/bin/env python3
"""
rack-elevation.py — A first-pass rack elevation from the BOM.

Takes every item with a rack-unit height, groups by location (one rack
per location), and stacks them — heaviest at the bottom by default — to
give you a U-numbered layout before anyone opens a drawing:

    Rack 1 - Courtroom 3A   (45 RU, 7 used, 38 free)
         U7 | Crestron CP4N                              1 RU
         U6 | QSC Core 110f                              1 RU
         U5 | QSC SPA4-100                               1 RU
         U4 | QSC SPA4-100                               1 RU
         U3 | Netgear M4250-26G4F-PoE+                   1 RU
      U1-U2 | Extron DTP CrossPoint 84 4K                2 RU

Usage:
    python rack-elevation.py project-bom.xlsx --only rack
    python rack-elevation.py project-bom.csv --rack-ru 44 --order bom
    python rack-elevation.py project-bom.csv --vent-every 4
    python rack-elevation.py project-bom.csv --csv > elevations.csv

Options that shape the layout:
    --order heavy-bottom   heaviest first from U1 up; unknown weights go on top (default)
    --order bom            BOM order, first line at the top of the stack
    --vent-every N         leave 1 RU of vent/blank after every N RU of equipment
    --start-u N            first U to fill (leave room for a UPS or drawer below)

Rack height comes from --rack-ru, or is read from the rack's own BOM
line when its model or description says so ("45RU", "44U"). A rack that
overflows is reported and exits 1.

This is a starting point for the real elevation in Vectorworks, not a
replacement: it knows weight and height, not which gear needs to be at
eye level or next to a patch bay. The CSV has one row per device with
its U position, ready to paste into a worksheet.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, parse_map_args  # noqa: E402

RACK_HEIGHT_RX = re.compile(r"\b(\d{1,2})\s*-?\s*(?:RU|U)\b", re.I)
RACK_RX = re.compile(r"\brack\b|\benclosure\b|\bcabinet\b", re.I)


def find_rack_height(items) -> float | None:
    """Height of the rack itself, if a line in this group is the rack and says its size."""
    for it in items:
        text = f"{it.category} {it.description}"
        if RACK_RX.search(text) and not it.rack_units:
            m = RACK_HEIGHT_RX.search(f"{it.description} {it.model}")
            if m and 4 <= int(m.group(1)) <= 58:
                return float(m.group(1))
    return None


def build_elevations(items, group_by="location", only=None, rack_ru=None, order="heavy-bottom",
                     vent_every=0, start_u=1) -> list:
    """Return one dict per rack: name, height, used, free, overflow, slots (top first)."""
    groups = {}
    for it in items:
        name = getattr(it, group_by) or "(none)"
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        groups.setdefault(name, []).append(it)

    racks = []
    for name, members in groups.items():
        units = []
        for it in members:
            ru = it.rack_units or 0
            if ru <= 0:
                continue
            for n in range(max(1, int(round(it.quantity)))):
                label = it.component_id if (it.component_id and it.quantity <= 1) else ""
                units.append({"label": it.label, "id": label, "ru": int(round(ru)) or 1,
                              "weight": it.weight, "manufacturer": it.manufacturer, "model": it.model,
                              "_n": n})
        if not units:
            continue
        if order == "heavy-bottom":       # heaviest first; unknown weight last (ends up on top)
            units.sort(key=lambda u: (u["weight"] is None, -(u["weight"] or 0), u["label"]))
        else:                             # BOM order reads top-down, so stack it bottom-up reversed
            units.reverse()

        slots, u, since_vent = [], start_u, 0
        for unit in units:
            if vent_every and since_vent >= vent_every:
                slots.append({"label": "(vent / blank)", "id": "", "ru": 1, "u_low": u, "u_high": u,
                              "manufacturer": "", "model": "", "blank": True})
                u, since_vent = u + 1, 0
            slots.append({**{k: unit[k] for k in ("label", "id", "ru", "manufacturer", "model")},
                          "u_low": u, "u_high": u + unit["ru"] - 1, "blank": False})
            u += unit["ru"]
            since_vent += unit["ru"]
        top = u - 1
        height = rack_ru or find_rack_height(members)
        racks.append({"name": name, "height": height, "used": top,
                      "equipment_ru": sum(s["ru"] for s in slots if not s["blank"]),
                      "free": (height - top) if height else None,
                      "overflow": bool(height and top > height),
                      "slots": list(reversed(slots))})
    return sorted(racks, key=lambda r: r["name"].lower())


def _span(slot) -> str:
    return f"U{slot['u_low']}" if slot["u_low"] == slot["u_high"] else f"U{slot['u_low']}-U{slot['u_high']}"


def render_text(racks) -> str:
    lines = []
    for r in racks:
        if r["height"]:
            head = f"{r['name']}   ({r['height']:g} RU, {r['used']} used, {r['free']:g} free)"
        else:
            head = f"{r['name']}   ({r['used']} RU used — rack height unknown, pass --rack-ru)"
        lines += [head]
        if r["overflow"]:
            lines.append(f"  ✗ OVERFLOW: needs {r['used']} RU, rack is {r['height']:g} RU")
        width = max(len(_span(s)) for s in r["slots"])
        for s in r["slots"]:
            name = s["label"] + (f"  [{s['id']}]" if s["id"] else "")
            lines.append(f"  {_span(s):>{width}} | {name[:46]:<46} {s['ru']} RU")
        lines.append("")
    return "\n".join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser(
        description="Generate first-pass rack elevations from a BOM export.",
        epilog="Example: python rack-elevation.py project-bom.xlsx --only rack --vent-every 4")
    parser.add_argument("bom", help="BOM export (.csv, .tsv, .xlsx) with a rack-units column")
    parser.add_argument("--group-by", choices=["location", "system"], default="location")
    parser.add_argument("--only", action="append", default=[], metavar="TEXT",
                        help="Only groups whose name contains TEXT (repeatable)")
    parser.add_argument("--rack-ru", type=float, help="Rack height in RU (default: read from the rack's BOM line)")
    parser.add_argument("--order", choices=["heavy-bottom", "bom"], default="heavy-bottom")
    parser.add_argument("--vent-every", type=int, default=0, metavar="N",
                        help="Leave 1 RU blank after every N RU of equipment")
    parser.add_argument("--start-u", type=int, default=1, help="First U to fill (default 1)")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Output as JSON")
    out.add_argument("--csv", action="store_true", help="Output as CSV, one row per device")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        items, table = load_bom(args.bom, parse_map_args(args.map), args.sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if not table.has("rack_units"):
        print(f"Error: {args.bom} has no rack-units column.\n"
              f"  Columns found: {', '.join(table.columns.values())}\n"
              "  Add the Rack Units product field to the export, or use --map rack_units=\"...\"",
              file=sys.stderr)
        sys.exit(2)

    racks = build_elevations(items, args.group_by, args.only, args.rack_ru, args.order,
                             args.vent_every, max(1, args.start_u))
    if not racks:
        print("No rack-mounted items found (nothing with a rack-unit height in the selected groups).",
              file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(racks, indent=2))
    elif args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(["Rack", "U Low", "U High", "RU", "Manufacturer", "Model", "ID"])
        for r in racks:
            for s in r["slots"]:
                w.writerow([r["name"], s["u_low"], s["u_high"], s["ru"], s["manufacturer"],
                            s["model"] or s["label"], s["id"]])
    else:
        print(render_text(racks))
    sys.exit(1 if any(r["overflow"] for r in racks) else 0)


if __name__ == "__main__":
    main()
