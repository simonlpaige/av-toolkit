#!/usr/bin/env python3
"""
av-device-monitor.py — Lightweight uptime monitoring for AV gear.

Pings a list of AV devices on a regular interval and logs status.
Optional: sends alert via webhook (Slack, Telegram) when a device
goes down or comes back up. Runs indefinitely — use as a service
or scheduled task.

Usage:
    python device-monitor.py devices.json                    # JSON device list
    python device-monitor.py devices.json --interval 60     # check every 60s
    python device-monitor.py devices.json --once             # one-shot check
    python device-monitor.py devices.json --alert-url URL    # webhook on state change

Device list format (JSON):
[
  {"name": "Q-SYS Core 110f", "ip": "192.168.1.10", "port": 1702},
  {"name": "Extron DTP Switch", "ip": "192.168.1.20", "port": 23},
  {"name": "Biamp TesiraFORTE", "ip": "192.168.1.30", "port": 23},
  {"name": "Crestron CP4N",     "ip": "192.168.1.40", "port": 41795},
  {"name": "Dante Brooklyn II", "ip": "192.168.1.50", "port": 50005}
]

Simon L. Paige — AV Toolkit
License: MIT
"""

import socket
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from typing import Optional


class Device:
    def __init__(self, name: str, ip: str, port: int = 80, timeout: float = 3.0):
        self.name = name
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.online = False
        self.last_change = None
        self.consecutive_failures = 0

    def check(self) -> bool:
        """TCP connect check. Returns True if reachable."""
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout):
                pass
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def update(self) -> Optional[str]:
        """Check device, return alert message if state changed."""
        was_online = self.online
        self.online = self.check()

        if not was_online and self.online:
            self.consecutive_failures = 0
            return f"✓ UP: {self.name} ({self.ip}) is back online."
        elif was_online and not self.online:
            self.last_change = datetime.now()
            return f"✗ DOWN: {self.name} ({self.ip}) is unreachable."
        elif not self.online:
            self.consecutive_failures += 1
            if self.consecutive_failures == 3:
                return f"⚠ PERSISTENT: {self.name} ({self.ip}) down for {self.consecutive_failures} checks."
        else:
            self.consecutive_failures = 0

        return None


def load_devices(path: str, timeout: float) -> list[Device]:
    """Load device list from JSON file."""
    with open(path) as f:
        data = json.load(f)
    return [Device(
        name=d.get("name", d.get("ip", "?")),
        ip=d["ip"],
        port=d.get("port", 80),
        timeout=timeout,
    ) for d in data]


def send_alert(url: str, message: str):
    """Send alert via webhook (POST with JSON payload)."""
    try:
        payload = json.dumps({"text": message, "timestamp": datetime.now().isoformat()}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  Alert send failed: {e}", file=sys.stderr)


def status_line(device: Device) -> str:
    """Format a single device status."""
    icon = "●" if device.online else "○"
    return f"  {icon} {device.name:<30} {device.ip:<16} :{device.port:<6}"


def main():
    parser = argparse.ArgumentParser(
        description="Monitor AV device uptime via TCP connect checks.",
        epilog="Example: python device-monitor.py devices.json --interval 30"
    )
    parser.add_argument("devices", help="JSON device list file")
    parser.add_argument("--interval", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-device TCP timeout")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--alert-url", help="Webhook URL for state-change alerts")
    parser.add_argument("--json", action="store_true", help="Output device status as JSON")
    args = parser.parse_args()

    try:
        devices = load_devices(args.devices, args.timeout)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading device list: {e}", file=sys.stderr)
        sys.exit(1)

    if not devices:
        print("No devices in list.", file=sys.stderr)
        sys.exit(1)

    def run_check():
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Checking {len(devices)} device(s)...")

        online = 0
        for device in devices:
            alert = device.update()
            if device.online:
                online += 1
            print(status_line(device))
            if alert:
                print(f"    • {alert}")
                if args.alert_url:
                    send_alert(args.alert_url, alert)

        print(f"\n  {online}/{len(devices)} online")
        return online

    if args.once:
        online = run_check()
        if args.json:
            result = {d.name: {"ip": d.ip, "online": d.online, "failures": d.consecutive_failures} for d in devices}
            print(json.dumps(result, indent=2))
        sys.exit(0 if online == len(devices) else 1)

    # Continuous mode
    print(f"Monitoring {len(devices)} devices every {args.interval}s. Ctrl+C to stop.")
    try:
        while True:
            run_check()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
