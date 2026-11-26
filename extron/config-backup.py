#!/usr/bin/env python3
r"""
extron-config-backup — Snapshot Extron device settings and diff them later.

Opens one SIS session per device, records identity and network settings,
and saves a timestamped JSON file. Run it at handoff, run it again on a
service call, and --diff tells you what changed in between.

Usage:
    python config-backup.py 192.168.1.100               # single device
    python config-backup.py --batch devices.txt         # one IP per line
    python config-backup.py 192.168.1.100 --diff        # compare with last backup
    python config-backup.py 192.168.1.100 --queries in1608.txt

What gets recorded by default (read-only on every SIS product):
    I, N, Q, *Q, 1I, 2I         model, part number, firmware
    {esc}CN / CH / CI / CS / CG / DH   name, MAC, IP, mask, gateway, DHCP

Device state (ties, volume, mutes, presets) is model-specific, and on
many switchers a command that looks like a query is a *set* — "1Z" mutes
the audio, "1B" blanks the video. So nothing beyond the list above is
sent unless you supply it with --queries. The file is one query per line:

    # name = command      (\e = Escape, \r = Return)
    current_input = !
    volume        = V
    audio_mute    = Z
    video_mute    = B
    out1_tie      = 1!
    mic1_gain     = \eG40000AU\r

Check each command against the product's user guide before adding it.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import read_targets_file  # noqa: E402
from avlib.sis import (IDENTITY_QUERIES, SISDevice, decode_command,  # noqa: E402
                       describe_error, show_command)

BACKUP_DIR = Path(__file__).parent / "backups"

# Refuse --queries entries that are plainly not queries.
DANGEROUS = re.compile(r"ZXXX|ZQQQ|ZY|ZFFF|BOOT|ERASE", re.I)


def load_queries(path: str) -> dict:
    """Read a 'name = command' file of extra read-only queries."""
    queries = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{n}: expected 'name = command', got '{line}'")
        name, command = (part.strip() for part in line.split("=", 1))
        if DANGEROUS.search(command):
            raise ValueError(f"{path}:{n}: '{command}' resets, reboots or erases — refusing to send it")
        if name in IDENTITY_QUERIES or name in queries:
            raise ValueError(f"{path}:{n}: duplicate query name '{name}'")
        queries[name] = decode_command(command)
    return queries


def collect_device_config(host: str, port: int = 23, timeout: float = 3.0,
                          password=None, extra_queries=None) -> dict:
    """Query a device and return its config snapshot. Sets config['error'] on failure."""
    config = {
        "host": host,
        "port": port,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "", "firmware": "", "part_number": "",
        "raw": {},
        "unsupported": {},
    }
    try:
        with SISDevice(host, port, timeout, password) as dev:
            config["banner"] = dev.banner
            if dev.password_required and not dev.logged_in:
                config["error"] = ("password required" if password is None
                                   else "password rejected")
                return config
            dev.identify()
            if not dev.is_extron:
                config["error"] = "connected, but this does not look like an Extron SIS device"
                return config
            config.update(model=dev.model, firmware=dev.firmware, part_number=dev.part_number)
            for name, cmd in {**IDENTITY_QUERIES, **(extra_queries or {})}.items():
                resp = dev.query(cmd)
                err = describe_error(resp)
                if err or not resp:
                    config["unsupported"][name] = f"{show_command(cmd)} -> {err or 'no response'}"
                else:
                    config["raw"][name] = resp
    except OSError as e:
        config["error"] = f"cannot connect: {e}"
    return config


def _prefix(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-]", "_", host)


def save_backup(config: dict, host: str, backup_dir: Path = None) -> Path:
    """Save a device config backup. Returns the file written."""
    backup_dir = backup_dir or BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"{_prefix(host)}-{stamp}.json"
    n = 1
    while path.exists():                      # two backups inside one second
        n += 1
        path = backup_dir / f"{_prefix(host)}-{stamp}-{n}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def find_latest_backup(host: str, backup_dir: Path = None):
    """Most recent backup file for a device, or None."""
    backup_dir = backup_dir or BACKUP_DIR
    if not backup_dir.exists():
        return None
    files = sorted(backup_dir.glob(f"{_prefix(host)}-[0-9]*.json"),
                   key=lambda p: (p.stat().st_mtime, p.name))
    return files[-1] if files else None


def diff_configs(previous: dict, current: dict) -> list:
    """Return [(key, was, now)] for every value that differs."""
    changes = []
    for field in ("model", "firmware", "part_number"):
        if previous.get(field, "") != current.get(field, ""):
            changes.append((field, previous.get(field, ""), current.get(field, "")))
    prev_raw, curr_raw = previous.get("raw", {}), current.get("raw", {})
    for key in sorted(set(prev_raw) | set(curr_raw)):
        was, now = prev_raw.get(key), curr_raw.get(key)
        if was != now:
            changes.append((key, "(not recorded)" if was is None else was,
                            "(not recorded)" if now is None else now))
    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Backup Extron device configurations via SIS.",
        epilog="Example: python config-backup.py 192.168.1.100 --diff")
    parser.add_argument("target", nargs="?", help="Device IP address")
    parser.add_argument("--batch", help="File with one IP per line")
    parser.add_argument("--port", type=int, default=23, help="SIS port (default 23)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--password", help="SIS password, if the units are protected")
    parser.add_argument("--queries", help="File of extra model-specific queries (name = command)")
    parser.add_argument("--out-dir", type=Path, default=BACKUP_DIR,
                        help=f"Backup directory (default: {BACKUP_DIR})")
    parser.add_argument("--diff", action="store_true", help="Diff against the previous backup")
    parser.add_argument("--no-save", action="store_true", help="Query and diff only; write nothing")
    parser.add_argument("--json", action="store_true", help="Print snapshots as JSON on stdout")
    args = parser.parse_args()

    if args.batch:
        try:
            targets = [host for host, _ in read_targets_file(args.batch)]
        except OSError as e:
            parser.error(f"cannot read {args.batch}: {e}")
    elif args.target:
        targets = [args.target]
    else:
        parser.print_help()
        sys.exit(2)
    try:
        extra = load_queries(args.queries) if args.queries else {}
    except (OSError, ValueError) as e:
        parser.error(str(e))

    log = sys.stderr if args.json else sys.stdout
    snapshots, failed, changed = [], 0, 0
    for host in targets:
        print(f"\n{host}", file=log)
        config = collect_device_config(host, args.port, args.timeout, args.password, extra)
        snapshots.append(config)
        if "error" in config:
            failed += 1
            print(f"  ✗ {config['error']} — nothing saved", file=log)
            continue
        print(f"  {config['model'] or '(model unknown)'}  FW {config['firmware'] or '?'}  "
              f"P/N {config['part_number'] or '?'}  — {len(config['raw'])} values", file=log)
        for name, why in config["unsupported"].items():
            print(f"  · {name}: {why}", file=log)

        if args.diff:
            previous = find_latest_backup(host, args.out_dir)
            if previous is None:
                print("  No previous backup to compare against.", file=log)
            else:
                old = json.loads(previous.read_text(encoding="utf-8"))
                changes = diff_configs(old, config)
                print(f"  Compared with {previous.name} ({old.get('timestamp', '?')}):", file=log)
                if not changes:
                    print("  ✓ No changes.", file=log)
                else:
                    changed += 1
                    for key, was, now in changes:
                        print(f"  [CHANGED] {key}\n      was: {was}\n      now: {now}", file=log)
        if not args.no_save:
            print(f"  Saved: {save_backup(config, host, args.out_dir)}", file=log)

    if args.json:
        print(json.dumps(snapshots, indent=2))
    if len(targets) > 1:
        print(f"\n{len(targets) - failed}/{len(targets)} backed up"
              + (f", {changed} changed since last backup" if args.diff else ""), file=log)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
