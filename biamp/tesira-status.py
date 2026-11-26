#!/usr/bin/env python3
"""
tesira-status.py — Poll Biamp Tesira units for identity and active faults.

Connects over the Tesira Text Protocol (Telnet, port 23) and reads
hostname, serial number, firmware and the active fault list. Run it
across every room before a client walkthrough to catch a fault LED
before they do.

Usage:
    python tesira-status.py 192.168.1.50
    python tesira-status.py --batch room-list.txt --json
    python tesira-status.py 192.168.1.50 --monitor --interval 10
    python tesira-status.py 192.168.1.50 --blocks      # list instance tags

TTP commands used (all read-only):
    DEVICE get hostname | serialNumber | version | activeFaultList
    SESSION get aliases                     (--blocks)

Telnet must be enabled on the unit (Device Maintenance > Network
Settings). It is off by default on newer firmware.

Exit code is 0 only when every unit answered and reported no faults.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import read_targets_file, run_parallel  # noqa: E402
from avlib.tesira import Tesira, TesiraError, fault_names  # noqa: E402


@dataclass
class TesiraStatus:
    host: str
    port: int = 23
    label: str = ""
    reachable: bool = False
    hostname: str = ""
    serial: str = ""
    version: str = ""
    faults: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.reachable and not self.faults and not self.errors


def poll_device(host: str, port: int = 23, timeout: float = 3.0,
                with_blocks: bool = False, label: str = "") -> TesiraStatus:
    """Poll a single Tesira for identity and faults."""
    status = TesiraStatus(host=host, port=port, label=label)
    try:
        with Tesira(host, port, timeout) as dsp:
            status.reachable = True
            for attr, name in (("hostname", "hostname"), ("serial", "serialNumber"),
                               ("version", "version")):
                try:
                    setattr(status, attr, str(dsp.get("DEVICE", name)))
                except TesiraError as e:
                    status.errors.append(f"{name}: {e}")
            try:
                reply = dsp.raw("DEVICE get activeFaultList")
                if reply.startswith("+OK"):
                    status.faults = fault_names(reply)
                else:
                    status.errors.append(f"activeFaultList: {reply}")
            except TesiraError as e:
                status.errors.append(f"activeFaultList: {e}")
            if with_blocks:
                aliases = dsp.try_get("SESSION", "aliases", default=[])
                status.blocks = sorted(a for a in aliases if a not in ("DEVICE", "SESSION"))
    except OSError as e:
        status.errors.append(f"cannot connect: {e}")
    return status


def print_status(s: TesiraStatus):
    title = s.label or s.hostname or s.host
    if not s.reachable:
        print(f"✗ {title} ({s.host}) — {'; '.join(s.errors)}")
        return
    print(f"{'✓' if s.healthy else '✗'} {title} ({s.host})")
    print(f"    Hostname: {s.hostname}   Serial: {s.serial}   Firmware: {s.version}")
    for fault in s.faults:
        print(f"    ⚠ FAULT: {fault}")
    for err in s.errors:
        print(f"    ⚠ {err}")
    if s.blocks:
        print(f"    Instance tags ({len(s.blocks)}): {', '.join(s.blocks)}")


def monitor(targets, port: int, timeout: float, interval: int):
    """Poll forever, printing a line only when a unit's state changes."""
    print(f"Monitoring {len(targets)} unit(s) every {interval}s. Ctrl+C to stop.")
    last = {}
    try:
        while True:
            for s in run_parallel(lambda t: poll_device(t[0], port, timeout, label=t[1]), targets):
                state = ("down",) if not s.reachable else ("up", tuple(s.faults))
                if last.get(s.host) != state:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    name = s.label or s.hostname or s.host
                    if not s.reachable:
                        print(f"[{stamp}] ✗ {name} unreachable")
                    elif s.faults:
                        print(f"[{stamp}] ⚠ {name}: {'; '.join(s.faults)}")
                    else:
                        print(f"[{stamp}] ✓ {name} OK (FW {s.version})")
                    sys.stdout.flush()
                    last[s.host] = state
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Poll Biamp Tesira devices for status and health.",
        epilog="Example: python tesira-status.py 192.168.1.50 --json")
    parser.add_argument("target", nargs="?", help="Tesira device IP address")
    parser.add_argument("--batch", help="File with one IP per line (optional label after the IP)")
    parser.add_argument("--port", type=int, default=23, help="Tesira TTP port (default 23)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--blocks", action="store_true", help="Also list DSP instance tags")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring mode")
    parser.add_argument("--interval", type=int, default=10, help="Monitor interval in seconds")
    args = parser.parse_args()

    if args.batch:
        try:
            targets = read_targets_file(args.batch)
        except OSError as e:
            parser.error(f"cannot read {args.batch}: {e}")
    elif args.target:
        targets = [(args.target, "")]
    else:
        parser.print_help()
        sys.exit(2)

    if args.monitor:
        monitor(targets, args.port, args.timeout, args.interval)
        return

    results = run_parallel(
        lambda t: poll_device(t[0], args.port, args.timeout, args.blocks, t[1]), targets)
    if args.json:
        print(json.dumps([{**asdict(r), "healthy": r.healthy} for r in results], indent=2))
    else:
        for s in results:
            print_status(s)
        if len(results) > 1:
            print(f"\n{sum(r.healthy for r in results)}/{len(results)} healthy")
    sys.exit(0 if all(r.healthy for r in results) else 1)


if __name__ == "__main__":
    main()
