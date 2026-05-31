#!/usr/bin/env python3
"""
cable-schedule.py — Generate structured cable pull schedules from JSON.

Takes a JSON description of rooms, devices, and cable types and produces
a formatted cable schedule — the kind you hand to a crew on pull day.
Outputs tab-separated text (paste into Excel), Markdown table, or CSV.

Input JSON format:
{
  "project": "Jackson County Courthouse",
  "rooms": [
    {
      "name": "Courtroom 3A",
      "cables": [
        {"id": "C-3A-001", "type": "CAT6 STP", "from": "Rack PDU", "to": "Judge Bench LCD",
         "connector": "RJ45", "length_m": 12, "path": "overhead tray", "notes": "PoE+"},
        {"id": "C-3A-002", "type": "SDI RG6", "from": "Camera PTZ", "to": "Rack SDI Matrix",
         "connector": "BNC", "length_m": 18, "path": "wall conduit"}
      ]
    }
  ]
}

Usage:
    python cable-schedule.py courthouse.json
    python cable-schedule.py courthouse.json --format csv > schedule.csv
    python cable-schedule.py courthouse.json --summary

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import sys
import csv
import io
from datetime import datetime


def load_schedule(path: str) -> dict:
    """Load and validate a cable schedule JSON file."""
    with open(path) as f:
        data = json.load(f)

    if "rooms" not in data:
        raise ValueError("JSON must contain a 'rooms' array.")

    for room in data["rooms"]:
        if "name" not in room:
            room["name"] = "(unnamed)"
        if "cables" not in room:
            room["cables"] = []

        for cable in room["cables"]:
            cable.setdefault("id", "")
            cable.setdefault("type", "")
            cable.setdefault("from", "")
            cable.setdefault("to", "")
            cable.setdefault("connector", "")
            cable.setdefault("length_m", 0)
            cable.setdefault("length_ft", 0)
            cable.setdefault("path", "")
            cable.setdefault("notes", "")

            # Auto-convert meters to feet if not provided
            if cable["length_ft"] == 0 and cable["length_m"] > 0:
                cable["length_ft"] = round(cable["length_m"] * 3.281, 1)

    return data


def generate_summary(data: dict) -> str:
    """Generate a summary report with cable counts by type and room."""
    lines = []
    lines.append(f"CABLE SCHEDULE SUMMARY")
    lines.append(f"Project: {data.get('project', 'Untitled')}")
    lines.append(f"Date:    {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"Rooms:   {len(data['rooms'])}")
    lines.append("")

    total_cables = 0
    total_length_ft = 0.0
    type_counts = {}
    type_lengths = {}

    for room in data["rooms"]:
        room_count = len(room["cables"])
        room_length = sum(c["length_ft"] for c in room["cables"])
        total_cables += room_count
        total_length_ft += room_length

        lines.append(f"  {room['name']}: {room_count} cables, {room_length:.0f} ft")

        for cable in room["cables"]:
            ct = cable["type"]
            type_counts[ct] = type_counts.get(ct, 0) + 1
            type_lengths[ct] = type_lengths.get(ct, 0.0) + cable["length_ft"]

    lines.append("")
    lines.append(f"  TOTALS: {total_cables} cables, {total_length_ft:.0f} ft ({total_length_ft/3.281:.0f} m)")
    lines.append("")

    lines.append("  By cable type:")
    for ct in sorted(type_counts):
        lines.append(f"    {ct}: {type_counts[ct]} cables, {type_lengths[ct]:.0f} ft")

    # Notes
    notes = data.get("notes", [])
    if notes:
        lines.append("")
        lines.append("  Project notes:")
        for note in notes:
            lines.append(f"    • {note}")

    return "\n".join(lines)


def generate_table(data: dict, output_format: str = "tsv") -> str:
    """Generate a formatted cable schedule table."""
    columns = ["ID", "Room", "Type", "From", "To", "Connector", "Length (ft)", "Length (m)", "Path", "Notes"]
    rows = []

    for room in data["rooms"]:
        for cable in room["cables"]:
            rows.append([
                cable["id"],
                room["name"],
                cable["type"],
                cable["from"],
                cable["to"],
                cable["connector"],
                str(cable["length_ft"]),
                str(cable["length_m"]),
                cable["path"],
                cable["notes"],
            ])

    if output_format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)
        return buf.getvalue()

    elif output_format == "markdown":
        lines = [f"# Cable Schedule — {data.get('project', 'Untitled')}", ""]
        header = "| " + " | ".join(columns) + " |"
        sep = "|" + "|".join(["---"] * len(columns)) + "|"
        lines.append(header)
        lines.append(sep)
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    else:  # tsv
        lines = ["\t".join(columns)]
        for row in rows:
            lines.append("\t".join(row))
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate AV cable pull schedules from JSON.",
        epilog="Example: python cable-schedule.py project.json --format csv"
    )
    parser.add_argument("input", help="JSON cable schedule file")
    parser.add_argument("--format", choices=["tsv", "csv", "markdown"], default="tsv",
                        help="Output format (default: tsv)")
    parser.add_argument("--summary", action="store_true", help="Print summary only")
    parser.add_argument("--out", help="Write output to file instead of stdout")
    args = parser.parse_args()

    try:
        data = load_schedule(args.input)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.summary:
        output = generate_summary(data)
    else:
        output = generate_table(data, args.format)

    if args.out:
        with open(args.out, "w") as f:
            f.write(output)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(output)


# Example schedule for testing (run with: python cable-schedule.py --example)
if __name__ == "__main__":
    if "--example" in sys.argv:
        example = {
            "project": "Example AV Project",
            "rooms": [
                {
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
                    ]
                }
            ]
        }
        print(json.dumps(example, indent=2))
    else:
        main()
