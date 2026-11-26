#!/usr/bin/env python3
"""
cable-schedule.py — Cable pull schedules from JSON or a design-tool export.

Takes a cable list and produces what the crew needs on pull day: a
schedule, a bulk-cable takeoff, a label sheet, and a sanity check.

Input can be:
  - JSON (format below), or
  - a spreadsheet: a Vectorworks ConnectCAD circuit/cable report, a
    D-Tools wire export, or anything with from / to / type / length
    columns (.csv, .tsv, .xlsx). Headers are matched by name — "Src
    Device", "Destination", "Cable Type", "Wire Length"... — and --map
    handles the rest.

JSON format:
{
  "project": "Jackson County Courthouse",
  "rooms": [
    {
      "name": "Courtroom 3A",
      "cables": [
        {"id": "C-3A-001", "type": "CAT6 STP", "from": "Rack PDU", "to": "Judge Bench LCD",
         "connector": "RJ45", "length_m": 12, "path": "overhead tray", "notes": "PoE+"},
        {"id": "C-3A-002", "type": "SDI RG6", "from": "Camera PTZ", "to": "Rack SDI Matrix",
         "connector": "BNC", "length_ft": 60, "path": "wall conduit"}
      ]
    }
  ]
}

Usage:
    python cable-schedule.py courthouse.json
    python cable-schedule.py courthouse.json --format csv > schedule.csv
    python cable-schedule.py connectcad-circuits.csv --units m --format markdown
    python cable-schedule.py courthouse.json --summary --slack 15 --spool 1000
    python cable-schedule.py courthouse.json --check
    python cable-schedule.py courthouse.json --format labels > labels.csv
    python cable-schedule.py dtools-wires.xlsx --format json > courthouse.json
    python cable-schedule.py --example > example.json

--check flags: duplicate or missing cable IDs, missing ends, missing
type or length, a cable that starts and ends on the same device, and
runs past what the cable can do (category cable over 100 m, passive
HDMI over 15 m, passive USB over 5 m). Exit code 1 if it finds errors.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import io
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.tabular import FT_PER_M, load_table, parse_map_args, to_feet  # noqa: E402

CABLE_ALIASES = {
    "id": ["cable id", "cable no", "cable number", "wire id", "wire no", "wire number",
           "number", "circuit", "circuit number", "circuit no", "label", "tag"],
    "type": ["cable type", "wire type", "cable", "wire", "cable name", "wire name", "model"],
    "signal": ["signal type", "signal name"],
    "from": ["source", "src", "source device", "src device", "from device", "origin", "start"],
    "from_port": ["source socket", "src socket", "from port", "source port", "from socket",
                  "source connector", "output"],
    "to": ["destination", "dest", "dst", "destination device", "dst device", "dest device",
           "to device", "end"],
    "to_port": ["destination socket", "dst socket", "dest socket", "to port", "destination port",
                "to socket", "destination connector", "input"],
    "connector": ["connectors", "termination", "connector type"],
    "length": ["cable length", "wire length", "length ft", "length feet", "len", "run length",
               "length m", "length meters"],
    "room": ["room name", "location", "location name", "area", "layer"],
    "path": ["route", "pathway", "conduit", "routing"],
    "notes": ["note", "comments", "comment", "remarks", "description"],
}

EXAMPLE = {
    "project": "Example AV Project",
    "notes": ["All category cable plenum-rated", "Leave 10 ft service loop at rack"],
    "rooms": [{
        "name": "Conference Room A",
        "cables": [
            {"id": "C-A-001", "type": "CAT6 STP", "from": "Rack Switch", "to": "Table Box Left",
             "connector": "RJ45", "length_m": 15, "path": "floor box → overhead", "notes": "PoE"},
            {"id": "C-A-002", "type": "HDMI Fiber", "from": "Rack Matrix", "to": "Wall Display",
             "connector": "HDMI", "length_m": 20, "path": "wall conduit", "notes": "Active optical"},
            {"id": "C-A-003", "type": "XLR Balanced", "from": "Ceiling Mic Array", "to": "Rack DSP",
             "connector": "XLR-M/F", "length_m": 12, "path": "overhead tray", "notes": "Shielded pair"},
            {"id": "C-A-004", "type": "Speaker 2C", "from": "Rack Amp", "to": "Ceiling Speaker L",
             "connector": "Euroblock", "length_m": 10, "path": "overhead tray", "notes": "14 AWG"},
        ],
    }],
}

TEXT_FIELDS = ("id", "type", "signal", "from", "from_port", "to", "to_port", "connector", "path", "notes")


def _finish_cable(cable: dict) -> dict:
    """Fill defaults and make sure both length_ft and length_m are set."""
    for f in TEXT_FIELDS:
        cable[f] = str(cable.get(f) or "").strip()
    ft = to_feet(cable.get("length_ft"), "ft") or 0.0
    m = (to_feet(cable.get("length_m"), "m") or 0.0) / FT_PER_M
    if not ft and m:
        ft = m * FT_PER_M
    elif ft and not m:
        m = ft / FT_PER_M
    cable["length_ft"], cable["length_m"] = round(ft, 1), round(m, 1)
    return cable


def load_json(path) -> dict:
    """Load and normalise a cable schedule JSON file."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        raise ValueError("JSON must be an object containing a 'rooms' array.")
    for room in data["rooms"]:
        room.setdefault("name", "(unnamed)")
        room["cables"] = [_finish_cable(dict(c)) for c in room.get("cables") or []]
    return data


def load_spreadsheet(path, overrides=None, sheet=None, units=None, project=None) -> dict:
    """Load a ConnectCAD / D-Tools / generic cable list into the schedule structure."""
    table = load_table(path, CABLE_ALIASES, required=[("from", "to")], overrides=overrides, sheet=sheet)
    if units is None:      # a header like "Length (m)" settles it
        header = table.columns.get("length", "").lower()
        units = "m" if re.search(r"\(\s*m\s*\)|\bmeters?\b|\bmetres?\b|\bm$", header) else "ft"
    rooms = {}
    for r in table.rows:
        if not (r.get("from") or r.get("to") or r.get("id")):
            continue
        cable = {f: r.get(f, "") for f in TEXT_FIELDS}
        feet = to_feet(r.get("length", ""), units)
        cable["length_ft"] = feet or 0.0
        rooms.setdefault(r.get("room") or "(no room)", []).append(_finish_cable(cable))
    return {"project": project or Path(path).stem,
            "rooms": [{"name": name, "cables": cables} for name, cables in rooms.items()]}


def load_schedule(path, overrides=None, sheet=None, units=None, project=None) -> dict:
    """Load a schedule from JSON or a spreadsheet, by file extension."""
    if str(path).lower().endswith(".json"):
        data = load_json(path)
    else:
        data = load_spreadsheet(path, overrides, sheet, units, project)
    if project:
        data["project"] = project
    return data


def all_cables(data):
    for room in data["rooms"]:
        for cable in room["cables"]:
            yield room["name"], cable


def _end(device, port):
    return f"{device} [{port}]" if port else device


# ---------------------------------------------------------------- checking

CATEGORY_RX = re.compile(r"cat\s*-?\s*[5678]|category|hdbaset|\butp\b|\bstp\b|\bftp\b", re.I)
ACTIVE_RX = re.compile(r"fib(er|re)|optic|aoc|active|extend|hdbaset|booster", re.I)


def check_schedule(data) -> list:
    """Return [(level, cable id, message)] for everything that looks wrong."""
    findings = []
    seen = {}
    for room, c in all_cables(data):
        cid = c["id"] or f"(no ID: {c['from'] or '?'} → {c['to'] or '?'})"

        def add(level, msg, cid=cid, room=room):
            findings.append((level, cid, f"{msg}  [{room}]"))

        if not c["id"]:
            add("ERROR", "no cable ID")
        elif c["id"] in seen:
            add("ERROR", f"duplicate cable ID (also in {seen[c['id']]})")
        else:
            seen[c["id"]] = room
        if not c["from"] or not c["to"]:
            add("ERROR", f"missing {'source' if not c['from'] else 'destination'}")
        elif _end(c["from"], c["from_port"]) == _end(c["to"], c["to_port"]):
            add("ERROR", f"starts and ends at the same place ({c['from']})")
        if not c["type"]:
            add("WARNING", "no cable type")
        if c["length_ft"] <= 0:
            add("WARNING", "no length — excluded from the bulk takeoff")

        kind, metres = f"{c['type']} {c['signal']}", c["length_m"]
        if CATEGORY_RX.search(kind) and metres > 100:
            add("ERROR", f"{metres:g} m exceeds the 100 m channel limit for category cable")
        elif CATEGORY_RX.search(kind) and metres > 90:
            add("WARNING", f"{metres:g} m leaves no patch-cord allowance (90 m permanent link)")
        elif re.search(r"hdmi", kind, re.I) and not ACTIVE_RX.search(f"{kind} {c['notes']}") and metres > 15:
            add("WARNING", f"{metres:g} m is long for passive HDMI — use an extender or active optical")
        elif re.search(r"\busb", kind, re.I) and not ACTIVE_RX.search(f"{kind} {c['notes']}") and metres > 5:
            add("WARNING", f"{metres:g} m exceeds the 5 m passive USB limit — needs an extender")
    order = {"ERROR": 0, "WARNING": 1}
    return sorted(findings, key=lambda f: order[f[0]])


# --------------------------------------------------------------- reporting

def bulk_takeoff(data, slack_pct=0.0, spool_ft=None) -> list:
    """Per cable type: count, pulled feet, feet to order, spools."""
    types = {}
    for _, c in all_cables(data):
        t = types.setdefault(c["type"] or "(no type)", {"count": 0, "feet": 0.0})
        t["count"] += 1
        t["feet"] += c["length_ft"]
    rows = []
    for name in sorted(types):
        t = types[name]
        order = t["feet"] * (1 + slack_pct / 100.0)
        rows.append({"type": name, "count": t["count"], "feet": round(t["feet"], 1),
                     "order_feet": round(order, 1),
                     "spools": math.ceil(order / spool_ft) if spool_ft and order else None})
    return rows


def generate_summary(data: dict, slack_pct: float = 0.0, spool_ft: float = None) -> str:
    """Summary report: cable counts by room, bulk takeoff by type."""
    lines = ["CABLE SCHEDULE SUMMARY",
             f"Project: {data.get('project', 'Untitled')}",
             f"Date:    {datetime.now().strftime('%Y-%m-%d')}",
             f"Rooms:   {len(data['rooms'])}", ""]
    total_count, total_ft = 0, 0.0
    for room in data["rooms"]:
        feet = sum(c["length_ft"] for c in room["cables"])
        total_count += len(room["cables"])
        total_ft += feet
        lines.append(f"  {room['name']}: {len(room['cables'])} cables, {feet:.0f} ft")
    lines += ["", f"  TOTALS: {total_count} cables, {total_ft:.0f} ft ({total_ft / FT_PER_M:.0f} m)", ""]

    head = "  By cable type"
    if slack_pct:
        head += f" (order quantity includes {slack_pct:g}% slack)"
    lines.append(head + ":")
    for row in bulk_takeoff(data, slack_pct, spool_ft):
        line = f"    {row['type']}: {row['count']} cables, {row['feet']:.0f} ft"
        if slack_pct:
            line += f" → order {row['order_feet']:.0f} ft"
        if row["spools"] is not None:
            line += f" ({row['spools']} × {spool_ft:g} ft)"
        lines.append(line)

    unmeasured = sum(1 for _, c in all_cables(data) if c["length_ft"] <= 0)
    if unmeasured:
        lines += ["", f"  ⚠ {unmeasured} cable(s) have no length and are not in the footage above."]
    if data.get("notes"):
        lines += ["", "  Project notes:"] + [f"    • {note}" for note in data["notes"]]
    return "\n".join(lines)


def _flat(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def generate_table(data: dict, output_format: str = "tsv") -> str:
    """Render the schedule as tsv, csv, markdown, labels (csv) or json."""
    if output_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    if output_format == "labels":
        # Two labels per cable — one for each end — for a label printer's CSV import.
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["Cable ID", "This End", "Far End", "Type", "Room"])
        for room, c in all_cables(data):
            a, b = _end(c["from"], c["from_port"]), _end(c["to"], c["to_port"])
            writer.writerow([c["id"], a, b, c["type"], room])
            writer.writerow([c["id"], b, a, c["type"], room])
        return buf.getvalue().rstrip("\n")

    cables = list(all_cables(data))
    columns = ["ID", "Room", "Type", "From", "To", "Connector", "Length (ft)", "Length (m)", "Path", "Notes"]
    if any(c["signal"] for _, c in cables):
        columns.insert(3, "Signal")
    rows = []
    for room, c in cables:
        row = {"ID": c["id"], "Room": room, "Type": c["type"], "Signal": c["signal"],
               "From": _end(c["from"], c["from_port"]), "To": _end(c["to"], c["to_port"]),
               "Connector": c["connector"], "Length (ft)": f"{c['length_ft']:g}",
               "Length (m)": f"{c['length_m']:g}", "Path": c["path"], "Notes": c["notes"]}
        rows.append([row[col] for col in columns])

    if output_format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return buf.getvalue().rstrip("\n")
    if output_format == "markdown":
        lines = [f"# Cable Schedule — {data.get('project', 'Untitled')}", "",
                 "| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
        lines += ["| " + " | ".join(_flat(v).replace("|", "\\|") for v in row) + " |" for row in rows]
        return "\n".join(lines)
    return "\n".join("\t".join(_flat(v) for v in row) for row in [columns] + rows)


def main():
    parser = argparse.ArgumentParser(
        description="Generate AV cable pull schedules from JSON or a ConnectCAD / D-Tools export.",
        epilog="Example: python cable-schedule.py project.json --format csv")
    parser.add_argument("input", nargs="?", help="Cable list: .json, .csv, .tsv or .xlsx")
    parser.add_argument("--format", choices=["tsv", "csv", "markdown", "labels", "json"], default="tsv",
                        help="Output format (default: tsv — pastes straight into Excel)")
    parser.add_argument("--summary", action="store_true", help="Print room totals and bulk takeoff")
    parser.add_argument("--check", action="store_true", help="Validate the schedule instead of printing it")
    parser.add_argument("--slack", type=float, default=0.0, metavar="PCT",
                        help="Waste / service-loop allowance for --summary, in percent")
    parser.add_argument("--spool", type=float, metavar="FT", help="Spool or box size in feet, for --summary")
    parser.add_argument("--room", action="append", default=[], metavar="TEXT",
                        help="Only rooms whose name contains TEXT (repeatable)")
    parser.add_argument("--units", choices=["ft", "m"],
                        help="Unit for bare lengths in a spreadsheet (default: from the header, else ft)")
    parser.add_argument("--project", help="Project name for headers")
    parser.add_argument("--map", action="append", default=[], metavar='FIELD="Column"',
                        help="Use this spreadsheet column for a field, e.g. --map from=\"Src Dev\"")
    parser.add_argument("--sheet", help="Worksheet name for .xlsx input")
    parser.add_argument("--out", help="Write output to file instead of stdout")
    parser.add_argument("--example", action="store_true", help="Print an example JSON schedule and exit")
    args = parser.parse_args()

    if args.example:
        print(json.dumps(EXAMPLE, indent=2, ensure_ascii=False))
        return
    if not args.input:
        parser.error("an input file is required (or --example)")

    try:
        data = load_schedule(args.input, parse_map_args(args.map), args.sheet, args.units, args.project)
    except (OSError, json.JSONDecodeError, ValueError) as e:      # TableError is a ValueError
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if args.room:
        data["rooms"] = [r for r in data["rooms"]
                         if any(t.lower() in r["name"].lower() for t in args.room)]

    exit_code = 0
    if args.check:
        findings = check_schedule(data)
        count = sum(1 for _ in all_cables(data))
        lines = [f"CABLE SCHEDULE CHECK — {data.get('project', 'Untitled')} ({count} cables)"]
        lines += [f"  {level:<8} {cid:<22} {msg}" for level, cid, msg in findings]
        errors = sum(1 for f in findings if f[0] == "ERROR")
        lines.append(f"\n  {errors} error(s), {len(findings) - errors} warning(s)" if findings
                     else "  ✓ No problems found.")
        output = "\n".join(lines)
        exit_code = 1 if errors else 0
    elif args.summary:
        output = generate_summary(data, args.slack, args.spool)
    else:
        output = generate_table(data, args.format)

    if args.out:
        Path(args.out).write_text(output + "\n", encoding="utf-8")
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(output)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
