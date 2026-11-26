#!/usr/bin/env python3
"""
av-device-monitor.py — Lightweight uptime monitoring for AV gear.

Checks a list of AV devices on an interval with a TCP connect to each
one's control port, logs state changes, and optionally posts them to a
webhook. Runs until stopped — use as a service or scheduled task.

Usage:
    python device-monitor.py devices.json                    # JSON device list
    python device-monitor.py devices.csv --interval 60       # CSV works too
    python device-monitor.py devices.json --once             # one-shot check
    python device-monitor.py devices.json --once --json      # ...as JSON
    python device-monitor.py devices.json --alert-url URL    # webhook on state change
    python device-monitor.py devices.json --log uptime.csv   # append changes to a CSV log

Device list (JSON):
[
  {"name": "Q-SYS Core 110f", "ip": "192.168.1.10", "port": 1710},
  {"name": "Extron DTP Switch", "ip": "192.168.1.20", "port": 23},
  {"name": "Biamp TesiraFORTE", "ip": "192.168.1.30", "port": 23},
  {"name": "Crestron CP4N",     "ip": "192.168.1.40", "port": 41794}
]
...or a CSV / spreadsheet with name, ip and port columns (an IP schedule
exported from D-Tools or Vectorworks will do). Port defaults to 80.

Alerts: a device is reported DOWN only after --fail-after consecutive
failed checks (default 2), so one dropped packet doesn't page anyone.
The webhook gets a JSON POST {"text": ..., "device": ..., "ip": ...,
"state": "down"|"up", "timestamp": ...} — the "text" field is what Slack
and Teams incoming webhooks display.

Exit code with --once: 0 if everything is up, 1 otherwise.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import json
import socket
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import run_parallel  # noqa: E402
from avlib.tabular import load_table, to_number  # noqa: E402

DEVICE_ALIASES = {
    "name": ["device", "device name", "hostname", "host name", "description", "component id", "tag"],
    "ip": ["ip address", "address", "ip addr", "host", "ipv4", "ipv4 address"],
    "port": ["tcp port", "control port"],
}


class Device:
    def __init__(self, name: str, ip: str, port: int = 80, timeout: float = 3.0, fail_after: int = 2):
        self.name, self.ip, self.port = name, ip, port
        self.timeout = timeout
        self.fail_after = max(1, fail_after)
        self.online: Optional[bool] = None       # None until the first check
        self.reported_down = False
        self.consecutive_failures = 0
        self.last_change: Optional[datetime] = None

    def check(self) -> bool:
        """TCP connect check. Returns True if reachable."""
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout):
                return True
        except OSError:
            return False

    def update(self, reachable: Optional[bool] = None) -> Optional[str]:
        """Record a check result; return an alert message if the state changed."""
        first = self.online is None
        self.online = self.check() if reachable is None else reachable
        if self.online:
            was_down = self.reported_down
            outage = self.last_change
            self.consecutive_failures, self.reported_down = 0, False
            if was_down:
                self.last_change = datetime.now()
                mins = (self.last_change - outage).total_seconds() / 60 if outage else 0
                return f"✓ UP: {self.name} ({self.ip}:{self.port}) is back online after {mins:.0f} min."
            return None
        self.consecutive_failures += 1
        if not self.reported_down and (first or self.consecutive_failures >= self.fail_after):
            self.reported_down = True
            self.last_change = datetime.now()
            return f"✗ DOWN: {self.name} ({self.ip}:{self.port}) is unreachable."
        return None

    def as_dict(self) -> dict:
        return {"name": self.name, "ip": self.ip, "port": self.port, "online": bool(self.online),
                "failures": self.consecutive_failures}


def load_devices(path: str, timeout: float = 3.0, fail_after: int = 2) -> list:
    """Load a device list from JSON, CSV or .xlsx."""
    if str(path).lower().endswith(".json"):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("devices")
        if not isinstance(data, list):
            raise ValueError("device list must be a JSON array of {name, ip, port} objects")
    else:
        table = load_table(path, DEVICE_ALIASES, required=["ip"])
        data = [{"name": r.get("name", ""), "ip": r["ip"], "port": r.get("port", "")}
                for r in table.rows if r["ip"]]
    devices = []
    for n, d in enumerate(data, 1):
        if not isinstance(d, dict) or not str(d.get("ip", "")).strip():
            raise ValueError(f"device #{n} has no 'ip'")
        port = int(to_number(d.get("port"), 80) or 80)
        if not 0 < port < 65536:
            raise ValueError(f"device #{n} ({d['ip']}): port {port} is out of range")
        ip = str(d["ip"]).strip()
        devices.append(Device(str(d.get("name") or ip), ip, port, timeout, fail_after))
    return devices


def send_alert(url: str, message: str, device: Device = None):
    """POST an alert to a webhook. Failures are reported, never raised."""
    payload = {"text": message, "timestamp": datetime.now().isoformat(timespec="seconds")}
    if device:
        payload.update(device=device.name, ip=device.ip, port=device.port,
                       state="up" if device.online else "down")
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:
        print(f"  Alert send failed: {e}", file=sys.stderr)


def status_line(device: Device) -> str:
    icon = "●" if device.online else "○"
    note = "" if device.online else f"  DOWN ×{device.consecutive_failures}"
    return f"  {icon} {device.name[:30]:<30} {device.ip:<16} :{device.port:<6}{note}"


def run_check(devices, alert_url=None, log_path=None, quiet=False, alerts_to=None) -> int:
    """Check every device once (in parallel). Returns the number online."""
    results = run_parallel(lambda d: d.check(), devices, 32)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not quiet:
        print(f"\n[{stamp}] Checking {len(devices)} device(s)...")
    for device, reachable in zip(devices, results):
        alert = device.update(reachable)
        if not quiet:
            print(status_line(device))
        if alert:
            print(f"[{stamp}] {alert}" if quiet else f"    • {alert}", flush=True,
                  file=alerts_to or sys.stdout)
            if alert_url:
                send_alert(alert_url, alert, device)
            if log_path:
                new = not Path(log_path).exists()
                with open(log_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if new:
                        writer.writerow(["timestamp", "name", "ip", "port", "state"])
                    writer.writerow([stamp, device.name, device.ip, device.port,
                                     "up" if device.online else "down"])
    online = sum(1 for d in devices if d.online)
    if not quiet:
        print(f"\n  {online}/{len(devices)} online", flush=True)
    return online


def main():
    parser = argparse.ArgumentParser(
        description="Monitor AV device uptime via TCP connect checks.",
        epilog="Example: python device-monitor.py devices.json --interval 30")
    parser.add_argument("devices", help="Device list (.json, .csv, .xlsx)")
    parser.add_argument("--interval", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-device TCP timeout")
    parser.add_argument("--fail-after", type=int, default=2,
                        help="Failed checks in a row before a device is reported down (default 2)")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--quiet", action="store_true", help="Only print state changes")
    parser.add_argument("--alert-url", help="Webhook URL for state-change alerts")
    parser.add_argument("--log", help="Append state changes to this CSV file")
    parser.add_argument("--json", action="store_true", help="With --once: output status as JSON")
    args = parser.parse_args()

    try:
        devices = load_devices(args.devices, args.timeout, args.fail_after)
    except (OSError, json.JSONDecodeError, ValueError) as e:     # TableError is a ValueError
        print(f"Error loading device list: {e}", file=sys.stderr)
        sys.exit(2)
    if not devices:
        print("No devices in list.", file=sys.stderr)
        sys.exit(2)

    if args.once:
        online = run_check(devices, args.alert_url, args.log, quiet=args.json or args.quiet,
                           alerts_to=sys.stderr if args.json else None)
        if args.json:
            print(json.dumps([d.as_dict() for d in devices], indent=2))
        sys.exit(0 if online == len(devices) else 1)

    print(f"Monitoring {len(devices)} devices every {args.interval}s. Ctrl+C to stop.")
    try:
        while True:
            started = time.monotonic()
            run_check(devices, args.alert_url, args.log, args.quiet)
            time.sleep(max(1.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
