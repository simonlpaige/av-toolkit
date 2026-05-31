#!/usr/bin/env python3
"""
tesira-status.py — Poll Biamp Tesira units for health and configuration.

Connects to Tesira devices over TCP (port 23) using the Tesira Text
Protocol (TTP) and queries device identity, audio status, and error
conditions. Use before client walkthrough to catch pre-existing faults.

Usage:
    python tesira-status.py 192.168.1.50
    python tesira-status.py --batch room-list.txt --json
    python tesira-status.py 192.168.1.50 --monitor --interval 10

Biamp Tesira TTP basics:
    - TCP port 23, plaintext commands terminated with \r\n
    - "DEVICE get version\r\n" returns firmware version
    - "DEVICE get alias\r\n" returns device name
    - "DEVICE get status\r\n" returns operational status
    - "SESSION get numActive\r\n" returns active VoIP/SIP sessions

Simon L. Paige — AV Toolkit
License: MIT
"""

import socket
import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class TesiraStatus:
    host: str
    port: int
    alias: str = ""
    version: str = ""
    status: str = ""
    active_sessions: int = 0
    errors: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def ttp_command(host: str, command: str, port: int = 23, timeout: float = 3.0) -> str:
    """Send a TTP command to a Tesira device and return the response."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"{command}\r\n".encode("ascii"))
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n" in response or b"\n" in response:
                    break
            return response.decode("ascii", errors="replace").strip()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return f"ERROR: {e}"


def parse_tesira_response(response: str) -> str:
    """Strip TTP prefix like '+OK \"value\"' or '-ERR message'."""
    if response.startswith("+OK"):
        # Strip +OK and quotes
        val = response[3:].strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        return val
    elif response.startswith("-ERR"):
        return f"ERROR: {response}"
    return response


def poll_device(host: str, port: int = 23, timeout: float = 3.0) -> TesiraStatus:
    """Poll a single Tesira device for full status."""
    status = TesiraStatus(host=host, port=port)

    queries = {
        "alias": "DEVICE get alias",
        "version": "DEVICE get version",
        "status": "DEVICE get status",
        "sessions": "SESSION get numActive",
    }

    for key, cmd in queries.items():
        resp = ttp_command(host, cmd, port, timeout)
        parsed = parse_tesira_response(resp)

        if parsed.startswith("ERROR"):
            status.errors.append(f"{key}: {parsed}")
        else:
            if key == "alias":
                status.alias = parsed
            elif key == "version":
                status.version = parsed
            elif key == "status":
                status.status = parsed
            elif key == "sessions":
                try:
                    status.active_sessions = int(parsed)
                except ValueError:
                    pass

    return status


def monitor_device(host: str, port: int, interval: int):
    """Continuous monitoring mode."""
    print(f"Monitoring {host} every {interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            s = poll_device(host, port)
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {s.alias} — Status: {s.status} — Sessions: {s.active_sessions} — FW: {s.version}")
            if s.errors:
                for err in s.errors:
                    print(f"  ⚠ {err}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Poll Biamp Tesira devices for status and health.",
        epilog="Example: python tesira-status.py 192.168.1.50 --json"
    )
    parser.add_argument("target", nargs="?", help="Tesira device IP address")
    parser.add_argument("--batch", help="File with one IP per line")
    parser.add_argument("--port", type=int, default=23, help="Tesira TCP port (default 23)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring mode")
    parser.add_argument("--interval", type=int, default=10, help="Monitor interval in seconds")
    args = parser.parse_args()

    targets = []
    if args.batch:
        with open(args.batch) as f:
            targets = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.target:
        targets = [args.target]
    else:
        parser.print_help()
        sys.exit(1)

    if args.monitor:
        monitor_device(targets[0], args.port, args.interval)
        return

    results = []
    for host in targets:
        status = poll_device(host, args.port, args.timeout)
        results.append(status)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for s in results:
            print(f"\n{'='*50}")
            print(f"Device: {s.alias or s.host}")
            print(f"  IP:       {s.host}")
            print(f"  Version:  {s.version}")
            print(f"  Status:   {s.status}")
            print(f"  Sessions: {s.active_sessions}")
            if s.errors:
                print(f"  Errors:")
                for err in s.errors:
                    print(f"    ⚠ {err}")


if __name__ == "__main__":
    main()
