#!/usr/bin/env python3
"""
companion-check.py — Find the accessory the BOM forgot.

Two 85" displays and no mounts. A rack and no power distribution. A
projector and no screen. Nothing is mispriced, so a price audit passes
— and the missing part shows up as a truck roll during install. This
checks a BOM export against "if you have X you need Y" rules.

Usage:
    python companion-check.py project-bom.xlsx
    python companion-check.py project-bom.csv --rules my-rules.json
    python companion-check.py project-bom.csv --print-rules > my-rules.json
    python companion-check.py project-bom.csv --json

Built-in rules:
    Displays need mounts            one mount, cart or stand per display, same location
    Projectors need mounts          one per projector, same location
    Projectors need screens         one per projector, same location
    Racks need power distribution   a PDU, power strip, conditioner or UPS per rack
    Speakers need amplification     at least one amplifier on the project
    Amplifiers need speakers        at least one loudspeaker on the project

A mount is only counted once: mounts used up by the displays are not
available to the projectors.

When the accessory exists but sits under a different location in the
BOM, that is reported as a note, not an error — it is priced, just
filed elsewhere.

Rules file (--rules) — a JSON list. --print-rules writes the built-in
set as a starting point:
    {"name": "Touch panels need table kits",
     "when": ["*touch*panel*"], "when_not": ["*wall*"],
     "needs": ["*tabletop kit*", "*table kit*"], "needs_not": [],
     "scope": "location",        "location" or "project"
     "mode": "each"}             "each" = one per item, "any" = at least one
Patterns are wildcards matched against "category | description | model",
ignoring case. Add --no-defaults to use only your own rules.

Exit code: 0 nothing missing, 1 something missing, 2 could not read the file.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import fnmatch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import add_table_args, load_bom  # noqa: E402
from avlib.tabular import parse_map_args  # noqa: E402

_NOT_HARDWARE = ["*cable*", "*adapter*", "*license*", "*warranty*", "*labor*", "*connector*"]
_MOUNTS = ["*mount*", "*bracket*", "*cart*", "*stand*"]

DEFAULT_RULES = [
    {"name": "Displays need mounts",
     "when": ["*display*", "*monitor*", "*flat panel*", "*television*", "* tv*"],
     "when_not": _MOUNTS + _NOT_HARDWARE + ["*displayport*", "*monitor speaker*", "*studio monitor*",
                                           "*touch panel*", "*touch screen*", "*processor*", "*controller*"],
     "needs": _MOUNTS, "needs_not": ["*rack mount*", "*rackmount*", "*projector*", "*camera*",
                                     "*speaker*", "*microphone*"],
     "scope": "location", "mode": "each"},
    {"name": "Projectors need mounts",
     "when": ["*projector*"],
     "when_not": _MOUNTS + _NOT_HARDWARE + ["*screen*", "*lens*", "*lamp*", "*lift*"],
     "needs": ["*mount*", "*bracket*", "*lift*"], "needs_not": ["*rack mount*", "*rackmount*", "*camera*",
                                                                "*speaker*", "*microphone*"],
     "scope": "location", "mode": "each"},
    {"name": "Projectors need screens",
     "when": ["*projector*"],
     "when_not": _MOUNTS + _NOT_HARDWARE + ["*screen*", "*lens*", "*lamp*", "*lift*"],
     "needs": ["*screen*"], "needs_not": ["*touch*", "*screen share*", "*screws*"],
     "scope": "location", "mode": "each"},
    {"name": "Racks need power distribution",
     "when": ["*equipment rack*", "*floor rack*", "*wall rack*", "*rack enclosure*", "*ru rack*",
              "*u rack*", "*credenza rack*", "racks |*", "rack |*"],
     "when_not": ["*shelf*", "*shelves*", "*rack mount*", "*rackmount*", "*rack kit*", "*rack ears*",
                  "*blank*", "*vent*", "*screw*", "*drawer*", "*caster*", "*fan*", "*panel*", "*lacing*",
                  "*pdu*", "*power*"],
     "needs": ["*pdu*", "*power distribution*", "*power strip*", "*power conditioner*", "*sequencer*",
               "*ups*", "*uninterruptible*"], "needs_not": ["*cable*"],
     "scope": "location", "mode": "each"},
    {"name": "Speakers need amplification",
     "when": ["*speaker*", "*loudspeaker*"],
     "when_not": _MOUNTS + _NOT_HARDWARE + ["*powered*", "*active*", "*amplified*", "*speakerphone*",
                                            "*wire*", "*amplifier*", "*soundbar*", "*video bar*",
                                            "*tile bridge*", "*back can*", "*grille*"],
     "needs": ["*amplifier*", "*amp |*", "* amp *", "*amplifiers*"], "needs_not": _NOT_HARDWARE + ["*distribution amp*"],
     "scope": "project", "mode": "any"},
    {"name": "Amplifiers need speakers",
     "when": ["*amplifier*"],
     "when_not": _NOT_HARDWARE + ["*distribution amp*", "*headphone*", "*antenna*", "*rf *", "*video*",
                                  "*hdmi*", "*preamp*"],
     "needs": ["*speaker*", "*loudspeaker*", "*subwoofer*", "*line array*", "*column*"],
     "needs_not": _MOUNTS + _NOT_HARDWARE + ["*speakerphone*", "*wire*"],
     "scope": "project", "mode": "any"},
]


def _text(item) -> str:
    return f"{item.category} | {item.description} | {item.model}".lower()


def _matches(text, patterns, excluded) -> bool:
    return (any(fnmatch.fnmatch(text, p.lower()) for p in patterns)
            and not any(fnmatch.fnmatch(text, p.lower()) for p in excluded))


def validate_rules(rules):
    if not isinstance(rules, list):
        raise ValueError("rules file must be a JSON list of rule objects")
    for n, rule in enumerate(rules, 1):
        if not isinstance(rule, dict) or not rule.get("when") or not rule.get("needs"):
            raise ValueError(f"rule #{n} needs both 'when' and 'needs' pattern lists")
        rule.setdefault("name", f"Rule {n}")
        rule.setdefault("when_not", [])
        rule.setdefault("needs_not", [])
        rule.setdefault("scope", "location")
        rule.setdefault("mode", "each")
        if rule["scope"] not in ("location", "project") or rule["mode"] not in ("each", "any"):
            raise ValueError(f"rule '{rule['name']}': scope must be location/project, mode each/any")
    return rules


def check_companions(items, rules=None) -> list:
    """Return findings: {level, rule, location, items, need, have, message}."""
    rules = DEFAULT_RULES if rules is None else rules
    texts = [_text(it) for it in items]
    remaining = [max(it.quantity, 0.0) for it in items]      # accessory units not yet claimed
    findings = []

    for rule in rules:
        triggers = [i for i, t in enumerate(texts) if _matches(t, rule["when"], rule["when_not"])]
        supplies = [i for i, t in enumerate(texts) if _matches(t, rule["needs"], rule["needs_not"])]
        if not triggers:
            continue
        if rule["mode"] == "any":
            if not supplies:
                names = sorted({items[i].label for i in triggers})
                findings.append({"level": "ERROR", "rule": rule["name"], "location": "(project)",
                                 "items": names, "need": 1, "have": 0,
                                 "message": f"{', '.join(names[:4])}{' ...' if len(names) > 4 else ''} — "
                                            "and nothing on the project to go with it"})
            continue

        groups = {}
        for i in triggers:
            key = items[i].location if rule["scope"] == "location" else "(project)"
            groups.setdefault(key, []).append(i)
        short = []
        for location, members in groups.items():
            need = sum(items[i].quantity for i in members)
            have = 0.0
            for s in supplies:
                if rule["scope"] == "location" and items[s].location != location:
                    continue
                take = min(remaining[s], need - have)
                remaining[s] -= take
                have += take
                if have >= need:
                    break
            if have < need:
                short.append((location, members, need, have))
        # Whatever is still unclaimed elsewhere on the project may be the missing part, misfiled.
        for location, members, need, have in short:
            elsewhere = 0.0
            for s in supplies:
                take = min(remaining[s], need - have - elsewhere)
                remaining[s] -= take
                elsewhere += take
            names = sorted({items[i].label for i in members})
            missing = need - have - elsewhere
            where = location or "(no location)"
            if missing > 0:
                findings.append({"level": "ERROR", "rule": rule["name"], "location": where, "items": names,
                                 "need": need, "have": have + elsewhere,
                                 "message": f"{need:g} × {', '.join(names)} but only {have + elsewhere:g} "
                                            f"to go with {'them' if need > 1 else 'it'} — {missing:g} short"})
            else:
                findings.append({"level": "NOTE", "rule": rule["name"], "location": where, "items": names,
                                 "need": need, "have": have + elsewhere,
                                 "message": f"{need:g} × {', '.join(names)}: covered, but {elsewhere:g} of the "
                                            "accessories are filed under another location"})
    return findings


def main():
    parser = argparse.ArgumentParser(
        description="Check a BOM for missing companion items (mounts, screens, rack power...).",
        epilog="Example: python companion-check.py project-bom.xlsx")
    parser.add_argument("bom", nargs="?", help="BOM export (.csv, .tsv, .xlsx)")
    parser.add_argument("--rules", help="JSON file of extra rules")
    parser.add_argument("--no-defaults", action="store_true", help="Use only the rules in --rules")
    parser.add_argument("--print-rules", action="store_true", help="Print the built-in rules as JSON and exit")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    add_table_args(parser)
    args = parser.parse_args()

    if args.print_rules:
        print(json.dumps(DEFAULT_RULES, indent=2))
        return
    if not args.bom:
        parser.error("a BOM file is required (or --print-rules)")
    try:
        rules = [] if args.no_defaults else list(DEFAULT_RULES)
        if args.rules:
            rules += validate_rules(json.loads(Path(args.rules).read_text(encoding="utf-8-sig")))
        if not rules:
            parser.error("--no-defaults needs --rules")
        items, table = load_bom(args.bom, parse_map_args(args.map), args.sheet)
    except (OSError, ValueError) as e:              # TableError and JSON errors are ValueErrors
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if not (table.has("category") or table.has("description")):
        print("Warning: no category or description column — rules can only match on model numbers, "
              "so most will not fire.", file=sys.stderr)

    findings = check_companions(items, rules)
    errors = [f for f in findings if f["level"] == "ERROR"]
    if args.json:
        print(json.dumps({"file": str(args.bom), "rules": len(rules), "findings": findings}, indent=2))
    else:
        print(f"COMPANION CHECK — {table.path.name} ({len(items)} lines, {len(rules)} rules)")
        for f in findings:
            print(f"\n  {'✗' if f['level'] == 'ERROR' else '·'} {f['rule']}  [{f['location']}]")
            print(f"      {f['message']}")
        print(f"\n  {len(errors)} missing, {len(findings) - len(errors)} note(s)" if findings
              else "\n  ✓ Nothing missing.")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
