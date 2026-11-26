#!/usr/bin/env python3
"""
rack-loads.py — Power, heat, rack space and weight per rack, from the BOM.

The electrician wants circuits, the mechanical engineer wants BTU/hr,
and somebody has to know whether it all fits in 44 RU. If the BOM has
watts / amps / BTU / rack-unit / weight columns (D-Tools SI carries them
as product fields), this adds them up per location:

    Location             Items   Watts    Amps  BTU/hr    RU   Weight  20A circuits
    Rack 1 - Courtroom      14   2,140    17.8   7,302    31    212.0   2

Usage:
    python rack-loads.py project-bom.csv
    python rack-loads.py project-bom.xlsx --only rack --rack-ru 44
    python rack-loads.py project-bom.csv --group-by system --volts 208 --circuit 30
    python rack-loads.py project-bom.csv --csv > loads.csv

How the numbers are derived:
    Watts    from the watts column, else amps × volts
    Amps     watts ÷ volts
    BTU/hr   from the BTU column, else watts × 3.412
    Circuits amps ÷ (circuit size × 80%), rounded up — continuous-load rule

These are nameplate figures, so they overstate real draw; treat them as
the conservative number. Items with no power data are counted and
called out, because a rack total that silently skips the amplifiers is
worse than no total.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import add_table_args, load_bom  # noqa: E402
from avlib.tabular import TableError, parse_map_args  # noqa: E402

BTU_PER_WATT = 3.412


def compute_loads(items, group_by="location", volts=120.0, circuit=20.0, rack_ru=None, only=None):
    """Return one load record per group, largest draw first."""
    groups = {}
    for it in items:
        name = getattr(it, group_by) or "(none)"
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        g = groups.setdefault(name, {"name": name, "items": 0, "watts": 0.0, "btu": 0.0, "ru": 0.0,
                                     "weight": 0.0, "no_power_data": []})
        qty = it.quantity
        g["items"] += qty
        watts = it.watts if it.watts else (it.amps * volts if it.amps else 0.0)
        g["watts"] += watts * qty
        g["btu"] += (it.btu if it.btu else watts * BTU_PER_WATT) * qty
        g["ru"] += (it.rack_units or 0.0) * qty
        g["weight"] += (it.weight or 0.0) * qty
        if not watts and (it.rack_units or 0) > 0:
            g["no_power_data"].append(it.label)

    out = []
    for g in groups.values():
        g["amps"] = g["watts"] / volts
        g["circuits"] = math.ceil(g["amps"] / (circuit * 0.8) - 1e-9) if g["amps"] > 0 else 0
        g["warnings"] = []
        if rack_ru and g["ru"] > rack_ru:
            g["warnings"].append(f"needs {g['ru']:g} RU — over the {rack_ru:g} RU rack "
                                 f"by {g['ru'] - rack_ru:g}")
        elif rack_ru and g["ru"] > rack_ru * 0.9:
            g["warnings"].append(f"{g['ru']:g} of {rack_ru:g} RU used — no room for vents or growth")
        if g["no_power_data"]:
            n = len(g["no_power_data"])
            g["warnings"].append(f"{n} rack item(s) have no power data, totals are understated: "
                                 + ", ".join(sorted(set(g["no_power_data"]))[:6])
                                 + (" ..." if n > 6 else ""))
        for k in ("watts", "btu", "ru", "weight", "amps", "items"):
            g[k] = round(g[k], 1)
        out.append(g)
    return sorted(out, key=lambda g: (-g["watts"], g["name"]))


def main():
    parser = argparse.ArgumentParser(
        description="Total power, heat, rack units and weight per location from a BOM export.",
        epilog="Example: python rack-loads.py project-bom.csv --only rack --rack-ru 44")
    parser.add_argument("bom", help="BOM export (.csv, .tsv, .xlsx)")
    parser.add_argument("--group-by", choices=["location", "system", "phase"], default="location")
    parser.add_argument("--only", action="append", default=[], metavar="TEXT",
                        help="Only groups whose name contains TEXT (repeatable), e.g. --only rack")
    parser.add_argument("--volts", type=float, default=120.0, help="Supply voltage (default 120)")
    parser.add_argument("--circuit", type=float, default=20.0, help="Circuit size in amps (default 20)")
    parser.add_argument("--rack-ru", type=float, help="Rack height in RU; flags racks that overflow")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Output as JSON")
    out.add_argument("--csv", action="store_true", help="Output as CSV")
    add_table_args(parser)
    args = parser.parse_args()

    try:
        items, table = load_bom(args.bom, parse_map_args(args.map), args.sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    have = [f for f in ("watts", "amps", "btu", "rack_units", "weight") if table.has(f)]
    if not have:
        print(f"Error: {args.bom} has no watts, amps, BTU, rack-unit or weight column.\n"
              f"  Columns found: {', '.join(table.columns.values())}\n"
              "  Add those product fields to the export, or point at them with --map watts=\"...\"",
              file=sys.stderr)
        sys.exit(2)
    if not table.has(args.group_by):
        print(f"Warning: no '{args.group_by}' column — everything lands in one group.", file=sys.stderr)

    loads = compute_loads(items, args.group_by, args.volts, args.circuit, args.rack_ru, args.only)
    if args.json:
        print(json.dumps(loads, indent=2))
    elif args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow([args.group_by.title(), "Items", "Watts", "Amps", "BTU/hr", "RU", "Weight",
                    f"{args.circuit:g}A circuits", "Warnings"])
        for g in loads:
            w.writerow([g["name"], g["items"], g["watts"], g["amps"], g["btu"], g["ru"], g["weight"],
                        g["circuits"], "; ".join(g["warnings"])])
    else:
        print(f"RACK LOADS — {table.path.name}  ({args.volts:g} V, {args.circuit:g} A circuits at 80%)")
        print(f"  data columns: {', '.join(have)}\n")
        head = f"{args.circuit:g}A ckts"
        print(f"  {args.group_by.title():<30}{'Items':>6}{'Watts':>9}{'Amps':>7}{'BTU/hr':>9}"
              f"{'RU':>6}{'Weight':>8}{head:>10}")
        for g in loads:
            print(f"  {g['name'][:29]:<30}{g['items']:>6g}{g['watts']:>9,.0f}{g['amps']:>7.1f}"
                  f"{g['btu']:>9,.0f}{g['ru']:>6g}{g['weight']:>8g}{g['circuits']:>10}")
        for g in loads:
            for warning in g["warnings"]:
                print(f"\n  ⚠ {g['name']}: {warning}")
        if not loads:
            print("  (no groups matched)")
    sys.exit(1 if any("over the" in w for g in loads for w in g["warnings"]) else 0)


if __name__ == "__main__":
    main()
