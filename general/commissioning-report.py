#!/usr/bin/env python3
"""
commissioning-report.py — One handoff report from everything the toolkit found.

Every tool here can write JSON. Run them on site, drop the files in a
folder, and this assembles a single Markdown report with a pass/fail
summary on top and the detail underneath — the document you attach to
the closeout package, or diff against on the next service call.

Usage:
    python commissioning-report.py results/*.json --project "Courtroom 3A" --out report.md
    python commissioning-report.py results/ --project "Courtroom 3A"

    # ...where results/ was filled by, for example:
    python extron/device-scanner.py 192.168.1.0/24 --json   > results/extron.json
    python biamp/tesira-status.py --batch dsps.txt --json   > results/tesira.json
    python crestron/iptable-checker.py 192.168.1.10 --json  > results/crestron.json
    python qsys/qrc-snapshot.py 192.168.1.20 --save results/core.json
    python general/device-monitor.py devices.json --once --json > results/uptime.json
    python dtools/bom-audit.py bom.xlsx --json              > results/bom.json

Recognised automatically, by content: Extron scans and config backups,
Tesira status, Biamp and Q-SYS snapshots, Crestron IP table checks,
device-monitor status and BOM audits. Anything else is listed as
unrecognised rather than dropped.

Exit code: 0 everything passed, 1 something failed, 2 nothing readable.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"


def _cell(value) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _table(headers, rows) -> list:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def _is_list_of(data, *keys) -> bool:
    return (isinstance(data, list) and bool(data) and all(isinstance(d, dict) for d in data)
            and all(k in data[0] for k in keys))


# Each section function returns (title, status, one-line summary, markdown lines).

def tesira_status(data):
    bad = [d for d in data if not d.get("healthy")]
    rows = [[d.get("label") or d.get("hostname") or d["host"], d["host"], d.get("serial"), d.get("version"),
             "OK" if d.get("healthy") else "; ".join(d.get("faults", []) + d.get("errors", [])) or "FAULT"]
            for d in data]
    return ("Biamp Tesira status", FAIL if bad else PASS, f"{len(data) - len(bad)}/{len(data)} healthy",
            _table(["Unit", "IP", "Serial", "Firmware", "State"], rows))


def crestron_iptable(data):
    issues = sum(len(d.get("issues", [])) for d in data)
    lines = []
    for d in data:
        lines += [f"**{d['host']}** {d.get('firmware', '')}".rstrip(), ""]
        lines += _table(["IP ID", "Type", "Status", "Address", "Model"],
                        [[e["ip_id"], e["type"], e["status"], e["address"], e.get("model")]
                         for e in d.get("ip_table", [])]) if d.get("ip_table") else ["_No IP table read._"]
        lines += [""] + [f"- ⚠ {i}" for i in d.get("issues", [])] + [""]
    entries = sum(len(d.get("ip_table", [])) for d in data)
    return ("Crestron IP tables", FAIL if issues else PASS,
            f"{len(data)} processor(s), {entries} entries, {issues} issue(s)", lines)


def extron_scan(data):
    locked = sum(1 for d in data if d.get("locked"))
    rows = [[d["ip"], d.get("model"), d.get("firmware"), d.get("part_number"), d.get("name"),
             "password set" if d.get("locked") else ""] for d in data]
    return ("Extron devices found", INFO, f"{len(data)} device(s)" + (f", {locked} locked" if locked else ""),
            _table(["IP", "Model", "Firmware", "Part number", "Name", "Notes"], rows))


def extron_backup(data):
    failed = [d for d in data if d.get("error")]
    rows = [[d["host"], d.get("model"), d.get("firmware"), d.get("part_number"),
             d.get("raw", {}).get("unit_name"), d.get("raw", {}).get("mac_address"),
             d.get("error") or f"{len(d.get('raw', {}))} values"] for d in data]
    return ("Extron configuration backups", FAIL if failed else PASS,
            f"{len(data) - len(failed)}/{len(data)} backed up",
            _table(["IP", "Model", "Firmware", "Part number", "Name", "MAC", "Result"], rows))


def monitor_status(data):
    down = [d for d in data if not d.get("online")]
    rows = [[d["name"], d["ip"], d.get("port"), "online" if d.get("online") else "**DOWN**"] for d in data]
    return ("Device reachability", FAIL if down else PASS, f"{len(data) - len(down)}/{len(data)} online",
            _table(["Device", "IP", "Port", "State"], rows))


def bom_audit(data):
    counts, totals = data.get("counts", {}), data.get("totals", {})
    lines = [f"Sell ${totals.get('price', 0):,.2f} · cost ${totals.get('cost', 0):,.2f} · "
             f"margin {totals.get('margin_pct', 0)}% · {totals.get('hours', 0):g} labor hours", ""]
    shown = [f for f in data.get("findings", []) if f["level"] != "INFO"]
    if shown:
        lines += _table(["Level", "Row", "Item", "Finding"],
                        [[f["level"], f["row"], f["item"], f["message"]] for f in shown])
    return ("BOM audit", FAIL if counts.get("ERROR") else PASS,
            f"{counts.get('ERROR', 0)} error(s), {counts.get('WARNING', 0)} warning(s)", lines)


def qsys_snapshot(data):
    comps = data["components"]
    controls = sum(len(c.get("controls", {})) for c in comps.values())
    muted = sorted(f"{name} : {cname}" for name, c in comps.items() for cname, ctl in c["controls"].items()
                   if (cname.lower() == "mute" or cname.lower().endswith(".mute")) and ctl.get("value") == 1)
    ok = data.get("status_code") in (None, 0)
    lines = _table(["Design", "Platform", "State", "Status", "Components", "Controls", "Captured"],
                   [[data.get("design"), data.get("platform"), data.get("state"), data.get("status"),
                     len(comps), controls, data.get("captured")]])
    if muted:
        lines += ["", f"Muted at capture ({len(muted)}):"] + [f"- {m}" for m in muted]
    return (f"Q-SYS Core {data.get('host', '')}".strip(), PASS if ok else FAIL,
            f"status {data.get('status') or '?'}, {len(comps)} components, {len(muted)} muted", lines)


def tesira_snapshot(data):
    values = sum(len(b) for b in data["blocks"].values())
    muted = sorted(f"{tag} {attr}" for tag, b in data["blocks"].items() for attr, v in b.items()
                   if "mute" in attr.lower() and v is True)
    lines = _table(["Hostname", "Serial", "Firmware", "Blocks", "Values", "Captured"],
                   [[data.get("hostname"), data.get("serial"), data.get("version"),
                     len(data["blocks"]), values, data.get("captured")]])
    if muted:
        lines += ["", f"Muted at capture ({len(muted)}):"] + [f"- {m}" for m in muted]
    return (f"Biamp Tesira snapshot {data.get('host', '')}".strip(), INFO,
            f"{len(data['blocks'])} blocks, {len(muted)} muted", lines)


def classify(data):
    """Return the section builder for a parsed JSON document, or None."""
    if _is_list_of(data, "host", "healthy"):
        return tesira_status
    if _is_list_of(data, "host", "ip_table"):
        return crestron_iptable
    if _is_list_of(data, "ip", "part_number"):
        return extron_scan
    if _is_list_of(data, "host", "raw"):
        return extron_backup
    if _is_list_of(data, "ip", "online"):
        return monitor_status
    if isinstance(data, dict):
        if "findings" in data and "totals" in data:
            return bom_audit
        if isinstance(data.get("components"), dict):
            return qsys_snapshot
        if isinstance(data.get("blocks"), dict):
            return tesira_snapshot
        if "host" in data and "raw" in data:
            return lambda d: extron_backup([d])
    return None


def build_report(files, project="", engineer="") -> tuple:
    """Return (markdown, any_failed, sections_built)."""
    sections, skipped = [], []
    for path in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            skipped.append((path, f"could not be read ({e})"))
            continue
        builder = classify(data)
        if builder is None:
            skipped.append((path, "not a format this report recognises"))
            continue
        try:
            sections.append((Path(path).name, *builder(data)))
        except (KeyError, TypeError, AttributeError) as e:
            skipped.append((path, f"looked like {builder.__name__} output but is malformed ({e!r})"))

    failed = [s for s in sections if s[2] == FAIL]
    icon = {PASS: "✅", FAIL: "❌", INFO: "ℹ️"}
    md = [f"# Commissioning Report{' — ' + project if project else ''}", "",
          f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
          + (f" by {engineer}" if engineer else ""), "",
          f"**Result: {'FAIL — ' + str(len(failed)) + ' check(s) need attention' if failed else 'PASS'}**"
          if sections else "**No results.**", ""]
    if sections:
        md += ["## Summary", ""] + _table(
            ["", "Check", "Result", "Source"],
            [[icon[status], title, summary, f"`{source}`"] for source, title, status, summary, _ in sections])
    for source, title, status, summary, lines in sections:
        md += ["", f"## {icon[status]} {title}", "", f"{summary} · from `{source}`", ""] + lines
    if skipped:
        md += ["", "## Files not included", ""] + [f"- `{Path(p).name}` — {why}" for p, why in skipped]
    return "\n".join(md).rstrip() + "\n", bool(failed), len(sections)


def main():
    parser = argparse.ArgumentParser(
        description="Assemble the toolkit's JSON results into one Markdown commissioning report.",
        epilog='Example: python commissioning-report.py results/ --project "Courtroom 3A" --out report.md')
    parser.add_argument("inputs", nargs="+", help="JSON result files, or folders of them")
    parser.add_argument("--project", default="", help="Project or room name for the title")
    parser.add_argument("--engineer", default="", help="Name to put on the report")
    parser.add_argument("--out", help="Write the report to this file instead of stdout")
    args = parser.parse_args()

    files = []
    for item in args.inputs:
        path = Path(item)
        files += sorted(path.glob("*.json")) if path.is_dir() else [path]
    report, failed, built = build_report(files, args.project, args.engineer)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"{built} section(s) written to {args.out}", file=sys.stderr)
    else:
        print(report, end="")
    sys.exit(2 if not built else 1 if failed else 0)


if __name__ == "__main__":
    main()
