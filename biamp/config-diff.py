#!/usr/bin/env python3
"""
biamp-config-diff.py — Compare Biamp Tesira DSP settings between rooms.

Useful when deploying identical systems across multiple rooms. Reads
every instance tag from each unit, snapshots its levels, mutes, gains
and mixer crosspoints, and reports the differences. Catches the "Room 4
sounds different" problem before a client notices.

Usage:
    python config-diff.py 192.168.1.50 192.168.1.51        # first is the reference
    python config-diff.py --batch rooms.txt --reference 192.168.1.50
    python config-diff.py 192.168.1.50 --save room1.json   # snapshot only
    python config-diff.py room1.json 192.168.1.51          # diff against a saved snapshot
    python config-diff.py room1.json 192.168.1.50          # ...or the same room, months later

How it works:
    "SESSION get aliases" lists the instance tags in the running
    configuration. TTP doesn't say what kind of block a tag is, so each
    tag is probed with the common read attributes (level, mute, gain,
    crosspointLevelState, sourceSelection, ...) and whatever answers is
    recorded. Only `get` commands are sent. Works from the live device —
    no .tesira file needed.

    Blocks need an instance tag to be visible to TTP, and custom tags
    must match between rooms for the comparison to line up.

Exit code: 0 = all match, 1 = differences found, 2 = a device failed.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import read_targets_file  # noqa: E402
from avlib.tesira import Tesira, TesiraError  # noqa: E402

SCALARS = ("numChannels", "numInputs", "numOutputs", "numSources", "sourceSelection")
PER_CHANNEL = ("level", "mute", "gain", "phantomPower", "invert", "state")
PER_INPUT = ("inputLevel", "inputMute")
PER_OUTPUT = ("outputLevel", "outputMute")
MAX_CROSSPOINTS = 1024


def _walk(dsp, tag, attribute, count, limit):
    """Read attribute[1..count]; with no count, read until the block says stop."""
    values = {}
    for i in range(1, (count or limit) + 1):
        try:
            values[f"{attribute}[{i}]"] = dsp.get(tag, attribute, i)
        except TesiraError:
            break
    return values


def snapshot_block(dsp, tag: str, max_channels: int = 32) -> dict:
    """Probe one instance tag for everything it will tell us."""
    block = {}
    for attr in SCALARS:
        value = dsp.try_get(tag, attr)
        if value is not None:
            block[attr] = value

    def count(name):
        return block[name] if isinstance(block.get(name), int) else None

    for attr in PER_CHANNEL:
        block.update(_walk(dsp, tag, attr, count("numChannels"), max_channels))
    n_in, n_out = count("numInputs"), count("numOutputs")
    if n_in or n_out:
        for attr in PER_INPUT:
            block.update(_walk(dsp, tag, attr, n_in, max_channels))
        for attr in PER_OUTPUT:
            block.update(_walk(dsp, tag, attr, n_out, max_channels))
    if n_in and n_out and n_in * n_out <= MAX_CROSSPOINTS:
        for attr in ("crosspointLevelState", "crosspointLevel"):
            if dsp.try_get(tag, attr, 1, 1) is None:
                continue
            for i in range(1, n_in + 1):
                for o in range(1, n_out + 1):
                    value = dsp.try_get(tag, attr, i, o)
                    if value is not None:
                        block[f"{attr}[{i},{o}]"] = value
    return block


def get_device_snapshot(host: str, port: int = 23, timeout: float = 3.0,
                        only=None, max_channels: int = 32) -> dict:
    """Collect a configuration snapshot from a live Tesira."""
    snapshot = {"host": host, "hostname": "", "serial": "", "version": "",
                "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "blocks": {}}
    with Tesira(host, port, timeout) as dsp:
        snapshot["hostname"] = str(dsp.get("DEVICE", "hostname"))
        snapshot["serial"] = str(dsp.try_get("DEVICE", "serialNumber", default=""))
        snapshot["version"] = str(dsp.try_get("DEVICE", "version", default=""))
        tags = dsp.get("SESSION", "aliases")
        if not isinstance(tags, list):
            raise TesiraError(f"unexpected reply to SESSION get aliases: {tags}")
        for tag in sorted(tags):
            if tag in ("DEVICE", "SESSION") or (only and tag not in only):
                continue
            snapshot["blocks"][tag] = snapshot_block(dsp, tag, max_channels)
    return snapshot


def load_snapshot(target: str, port: int, timeout: float, only, max_channels: int) -> dict:
    """A target is a saved snapshot (.json) or a live device address."""
    if target.lower().endswith(".json"):
        data = json.loads(Path(target).read_text(encoding="utf-8"))
        if "blocks" not in data:
            raise ValueError(f"{target} is not a Tesira snapshot (no 'blocks')")
        data.setdefault("host", target)
        if only:
            data["blocks"] = {t: b for t, b in data["blocks"].items() if t in only}
        return data
    return get_device_snapshot(target, port, timeout, only, max_channels)


def _same(a, b, tolerance: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tolerance
    return a == b


def diff_snapshots(ref: dict, target: dict, tolerance: float = 0.05) -> list:
    """Compare two snapshots; return a list of difference dicts."""
    diffs = []

    def add(severity, param, reference, value):
        diffs.append({"severity": severity, "param": param, "reference": reference,
                      "target": value, "host": target.get("host", "")})

    if ref.get("version") != target.get("version"):
        add("warning", "firmware version", ref.get("version"), target.get("version"))

    ref_blocks, tgt_blocks = ref["blocks"], target["blocks"]
    for tag in sorted(set(ref_blocks) | set(tgt_blocks)):
        if tag not in tgt_blocks:
            add("missing", tag, "present", "no such instance tag")
            continue
        if tag not in ref_blocks:
            add("extra", tag, "no such instance tag", "present")
            continue
        a, b = ref_blocks[tag], tgt_blocks[tag]
        for attr in sorted(set(a) | set(b)):
            if attr not in a or attr not in b:
                add("mismatch", f"{tag} {attr}", a.get(attr, "(absent)"), b.get(attr, "(absent)"))
            elif not _same(a[attr], b[attr], tolerance):
                add("mismatch", f"{tag} {attr}", a[attr], b[attr])
    return diffs


def main():
    parser = argparse.ArgumentParser(
        description="Compare Biamp Tesira DSP configurations between devices.",
        epilog="Example: python config-diff.py 192.168.1.50 192.168.1.51")
    parser.add_argument("targets", nargs="*",
                        help="Device IPs or saved snapshot .json files (first is the reference)")
    parser.add_argument("--batch", help="File with one IP per line (first = reference)")
    parser.add_argument("--reference", help="Reference device (moves it to the front)")
    parser.add_argument("--save", metavar="FILE", help="Save the first target's snapshot as JSON")
    parser.add_argument("--blocks", help="Only compare these instance tags (comma-separated)")
    parser.add_argument("--tolerance", type=float, default=0.05,
                        help="Ignore numeric differences up to this many dB (default 0.05)")
    parser.add_argument("--max-channels", type=int, default=32,
                        help="Channel probe limit for blocks that don't report a count")
    parser.add_argument("--port", type=int, default=23, help="Tesira TTP port")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--json", action="store_true", help="Print differences as JSON on stdout")
    args = parser.parse_args()

    targets = list(args.targets)
    if args.batch:
        try:
            targets += [host for host, _ in read_targets_file(args.batch)]
        except OSError as e:
            parser.error(f"cannot read {args.batch}: {e}")
    if args.reference:
        if args.reference in targets:
            targets.remove(args.reference)
        targets.insert(0, args.reference)
    if not targets:
        parser.print_help()
        sys.exit(2)
    if len(targets) < 2 and not args.save:
        parser.error("need at least 2 devices to compare (reference + target), or --save FILE")

    only = {t.strip() for t in args.blocks.split(",") if t.strip()} if args.blocks else None
    log = sys.stderr if args.json else sys.stdout

    def capture(target):
        snap = load_snapshot(target, args.port, args.timeout, only, args.max_channels)
        values = sum(len(b) for b in snap["blocks"].values())
        print(f"  {snap.get('hostname') or target}: FW {snap.get('version') or '?'}, "
              f"{len(snap['blocks'])} blocks, {values} values", file=log)
        return snap

    print(f"Reference: {targets[0]}", file=log)
    try:
        ref = capture(targets[0])
    except (OSError, TesiraError, ValueError) as e:
        print(f"  ✗ {targets[0]}: {e}", file=sys.stderr)
        sys.exit(2)
    if not ref["blocks"]:
        print("  ⚠ No instance tags found — nothing to compare. Blocks need instance tags "
              "to be visible over TTP.", file=log)
    if args.save:
        Path(args.save).write_text(json.dumps(ref, indent=2), encoding="utf-8")
        print(f"  Snapshot saved: {args.save}", file=log)

    all_diffs, failed = {}, 0
    for target in targets[1:]:
        print(f"\nComparing: {target}", file=log)
        try:
            snap = capture(target)
        except (OSError, TesiraError, ValueError) as e:
            failed += 1
            print(f"  ✗ {e}", file=log)
            continue
        diffs = diff_snapshots(ref, snap, args.tolerance)
        all_diffs[target] = diffs
        if not diffs:
            print("  ✓ Matches reference.", file=log)
        else:
            print(f"  ✗ {len(diffs)} difference(s):", file=log)
            for d in diffs:
                print(f"    [{d['severity']}] {d['param']}: reference {d['reference']} "
                      f"→ this unit {d['target']}", file=log)

    if args.json:
        print(json.dumps(all_diffs, indent=2))
    sys.exit(2 if failed else 1 if any(all_diffs.values()) else 0)


if __name__ == "__main__":
    main()
