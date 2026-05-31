#!/usr/bin/env python3
"""
extron-device-scanner — Find every Extron device on a subnet.

Scans a subnet range via TCP port 23 (Telnet/SIS), sends the info
query command "I\r", and reports model, firmware, and part number.

Usage:
    python device-scanner.py 192.168.1.0/24
    python device-scanner.py 10.0.0.1-254 --timeout 2
    python device-scanner.py 192.168.1.0/24 --json > devices.json

Extron SIS over TCP:
    Port 23 (Telnet), or 2001 on some models.
    Commands use ASCII text terminated with carriage return.
    "I\r" returns model and firmware info.
    "N\r" returns part number.

Simon L. Paige — AV Toolkit
License: MIT
"""

import socket
import ipaddress
import argparse
import json
import sys
import concurrent.futures
from dataclasses import dataclass, asdict
from typing import Optional

# Extron devices commonly respond on these ports
SIS_PORTS = [23, 2001]


@dataclass
class ExtronDevice:
    ip: str
    model: str = ""
    firmware: str = ""
    part_number: str = ""
    port: int = 23


def sis_query(host: str, port: int, command: str, timeout: float = 2.0) -> Optional[str]:
    """Send an SIS command to an Extron device and return the response."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"{command}\r".encode("ascii"))
            response = b""
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response += chunk
                if b"\r\n" in response or b"\n" in response:
                    break
            return response.decode("ascii", errors="replace").strip()
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None


def probe_host(host: str, timeout: float) -> Optional[ExtronDevice]:
    """Try all known SIS ports on a single host."""
    for port in SIS_PORTS:
        info = sis_query(host, port, "I", timeout)
        if info and info != "E22":
            device = ExtronDevice(ip=host, port=port)
            # Parse model/firmware from "I" response
            # Typical format: "VXX AV MM/DD/YY-HH:MM:SS"
            parts = info.split()
            if parts:
                device.model = parts[0]
            device.firmware = " ".join(parts[1:]) if len(parts) > 1 else ""
            # Get part number
            pn = sis_query(host, port, "N", timeout)
            if pn and pn != "E22":
                device.part_number = pn
            return device
    return None


def scan_network(target: str, timeout: float, workers: int = 50) -> list:
    """Scan a network range for Extron devices."""
    ips = list(ipaddress.IPv4Network(target, strict=False).hosts()) if "/" in target else []
    if not ips and "-" in target:
        # Support range syntax: 192.168.1.1-254
        base_parts = target.split("-")[0].rsplit(".", 1)
        start, end = target.split("-")[-1].split("-")
        if len(start) <= 3:
            base = base_parts[0]
            ips = [ipaddress.IPv4Address(f"{base}.{i}") for i in range(int(start), int(end.split("-")[-1]) + 1)]
    if not ips:
        ips = [ipaddress.IPv4Address(target)]

    devices = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(probe_host, str(ip), timeout): ip for ip in ips}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                devices.append(result)
    return sorted(devices, key=lambda d: ipaddress.IPv4Address(d.ip))


def main():
    parser = argparse.ArgumentParser(
        description="Scan a network for Extron devices via SIS/Telnet.",
        epilog="Example: python device-scanner.py 192.168.1.0/24 --json"
    )
    parser.add_argument("target", help="IP, range (x.x.x.x-y), or CIDR subnet")
    parser.add_argument("--timeout", type=float, default=1.5, help="Per-host connection timeout in seconds")
    parser.add_argument("--workers", type=int, default=50, help="Concurrent scan workers")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    print(f"Scanning {args.target} for Extron devices...", file=sys.stderr)
    devices = scan_network(args.target, args.timeout, args.workers)

    if args.json:
        print(json.dumps([asdict(d) for d in devices], indent=2))
    else:
        if not devices:
            print("No Extron devices found.", file=sys.stderr)
            return
        print(f"\nFound {len(devices)} Extron device(s):\n")
        print(f"{'IP':<15} {'Port':<6} {'Model':<20} {'Firmware':<30} {'Part Number':<20}")
        print("-" * 91)
        for d in devices:
            print(f"{d.ip:<15} {d.port:<6} {d.model:<20} {d.firmware:<30} {d.part_number:<20}")


if __name__ == "__main__":
    main()
