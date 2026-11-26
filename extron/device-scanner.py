#!/usr/bin/env python3
"""
extron-device-scanner — Find every Extron device on a subnet.

Connects to TCP 23 on each address and reads the banner an Extron unit
sends on connect:

    (c) Copyright 2019, Extron Electronics, IN1608, V1.07, 60-1238-01

Model, firmware and part number come straight from that banner, so the
scan is passive for most devices. Units that stay silent are asked for
their part number (N) and firmware (Q) — both read-only.

Usage:
    python device-scanner.py 192.168.1.0/24
    python device-scanner.py 10.0.0.1-254 --timeout 2
    python device-scanner.py 192.168.1.0/24 --json > devices.json
    python device-scanner.py 192.168.1.0/24 --csv > devices.csv
    python device-scanner.py 192.168.1.0/24 --password extron

Notes:
    - Password-protected units still reveal model/firmware in the banner;
      they are listed with "locked" unless --password is given.
    - Only port 23 is probed by default. Ports 2001+ on IPL/IPCP boxes are
      serial pass-through to whatever is wired to the COM port, so this
      script never talks to them unless you ask with --port.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import ipaddress
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import TargetError, expand_targets, run_parallel  # noqa: E402
from avlib.sis import ESC, SISDevice, describe_error  # noqa: E402


@dataclass
class ExtronDevice:
    ip: str
    port: int = 23
    model: str = ""
    firmware: str = ""
    part_number: str = ""
    name: str = ""
    locked: bool = False


def probe_host(host: str, port: int = 23, timeout: float = 1.5,
               password: Optional[str] = None) -> Optional[ExtronDevice]:
    """Return an ExtronDevice if `host` answers like one, else None."""
    try:
        with SISDevice(host, port, timeout, password) as dev:
            if dev.banner and not dev.is_extron:
                return None                 # some other Telnet service
            if dev.logged_in:
                dev.identify()
            if not dev.is_extron:
                return None
            found = ExtronDevice(ip=host, port=port, model=dev.model, firmware=dev.firmware,
                                 part_number=dev.part_number,
                                 locked=dev.password_required and not dev.logged_in)
            if dev.logged_in:
                name = dev.query(ESC + "CN\r")
                if name and not describe_error(name):
                    found.name = name.splitlines()[0].removeprefix("Ipn ").strip()
            return found
    except OSError:
        return None


def scan_network(target: str, timeout: float = 1.5, workers: int = 50, port: int = 23,
                 password: Optional[str] = None) -> list:
    """Scan an IP, range or subnet for Extron devices."""
    hosts = expand_targets(target)
    found = run_parallel(lambda h: probe_host(h, port, timeout, password), hosts, workers)
    devices = [d for d in found if d]

    def sort_key(d):
        try:
            return (0, int(ipaddress.IPv4Address(d.ip)), "")
        except ipaddress.AddressValueError:
            return (1, 0, d.ip)
    return sorted(devices, key=sort_key)


def main():
    parser = argparse.ArgumentParser(
        description="Scan a network for Extron devices via SIS/Telnet.",
        epilog="Example: python device-scanner.py 192.168.1.0/24 --json")
    parser.add_argument("target", help="IP, range (x.x.x.x-y), or CIDR subnet")
    parser.add_argument("--port", type=int, default=23, help="SIS port (default 23)")
    parser.add_argument("--timeout", type=float, default=1.5, help="Per-host timeout in seconds")
    parser.add_argument("--workers", type=int, default=50, help="Concurrent scan workers")
    parser.add_argument("--password", help="SIS password to try on locked units")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Output as JSON")
    out.add_argument("--csv", action="store_true", help="Output as CSV")
    args = parser.parse_args()

    try:
        hosts = expand_targets(args.target)
    except TargetError as e:
        parser.error(str(e))
    print(f"Scanning {len(hosts)} address(es) for Extron devices...", file=sys.stderr)
    devices = scan_network(args.target, args.timeout, args.workers, args.port, args.password)

    if args.json:
        print(json.dumps([asdict(d) for d in devices], indent=2))
    elif args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["IP", "Port", "Model", "Firmware", "Part Number", "Name", "Locked"])
        for d in devices:
            writer.writerow([d.ip, d.port, d.model, d.firmware, d.part_number, d.name,
                             "yes" if d.locked else ""])
    elif devices:
        print(f"\nFound {len(devices)} Extron device(s):\n")
        print(f"{'IP':<16}{'Model':<26}{'Firmware':<12}{'Part Number':<14}{'Name':<24}Notes")
        print("-" * 98)
        for d in devices:
            print(f"{d.ip:<16}{d.model:<26}{d.firmware:<12}{d.part_number:<14}{d.name:<24}"
                  f"{'locked (password set)' if d.locked else ''}")
    else:
        print("No Extron devices found.", file=sys.stderr)
    sys.exit(0 if devices else 1)


if __name__ == "__main__":
    main()
