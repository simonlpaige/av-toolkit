#!/usr/bin/env python3
"""
extron-config-backup — Batch backup Extron device configurations.

Connects to Extron devices over TCP (SIS), dumps key configuration
parameters, and saves them as timestamped JSON files. Supports
firmware version tracking and comparative diff on re-run.

Usage:
    python config-backup.py 192.168.1.100              # single device
    python config-backup.py --batch devices.txt        # batch from IP list
    python config-backup.py 192.168.1.100 --diff       # compare with last backup

Extron SIS query commands used:
    I  — information (model, firmware)
    N  — part number
    0G — global preset status
    0Z — volume level
    1Z — audio input 1 level
    1B — input 1 video status

Simon L. Paige — AV Toolkit
License: MIT
"""

import socket
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path(__file__).parent / "backups"


def sis_query(host: str, command: str, port: int = 23, timeout: float = 3.0) -> str:
    """Send an SIS command to an Extron device and return the response."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"{command}\r".encode("ascii"))
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n" in response:
                    break
            return response.decode("ascii", errors="replace").strip()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return f"ERROR: {e}"


def collect_device_config(host: str, port: int = 23, timeout: float = 3.0) -> dict:
    """Query all available config data from an Extron device."""
    config = {
        "host": host,
        "port": port,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw": {}
    }

    queries = {
        "info": "I",
        "part_number": "N",
        "volume": "0Z",
        "input1_audio": "1Z",
        "input1_video": "1B",
        "input2_audio": "2Z",
        "input2_video": "2B",
        "input3_audio": "3Z",
        "input3_video": "3B",
        "preset_status": "0G",
        "mute_status": "0Z",
    }

    for name, cmd in queries.items():
        resp = sis_query(host, cmd, port, timeout)
        config["raw"][name] = resp

    return config


def save_backup(config: dict, device_ip: str):
    """Save a device config backup to the backups directory."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = BACKUP_DIR / f"{device_ip.replace('.', '_')}-{timestamp}.json"
    with open(filename, "w") as f:
        json.dump(config, f, indent=2)
    return filename


def find_latest_backup(device_ip: str) -> Path | None:
    """Find the most recent backup for a given device IP."""
    if not BACKUP_DIR.exists():
        return None
    prefix = device_ip.replace(".", "_")
    files = sorted(BACKUP_DIR.glob(f"{prefix}-*.json"), reverse=True)
    return files[0] if files else None


def diff_backups(current: dict, previous_path: Path):
    """Print a human-readable diff between current and previous backup."""
    with open(previous_path) as f:
        previous = json.load(f)

    print(f"\nComparing with: {previous_path.name}")
    print(f"Previous timestamp: {previous['timestamp']}")
    print(f"Current  timestamp: {current['timestamp']}\n")

    prev_raw = previous.get("raw", {})
    curr_raw = current.get("raw", {})

    for key in sorted(curr_raw):
        prev_val = prev_raw.get(key, "N/A")
        curr_val = curr_raw[key]
        if prev_val != curr_val:
            print(f"  [CHANGED] {key}:")
            print(f"    was: {prev_val}")
            print(f"    now: {curr_val}")
        else:
            print(f"  [OK]      {key}: {curr_val}")


def main():
    parser = argparse.ArgumentParser(
        description="Backup Extron device configurations via SIS.",
        epilog="Example: python config-backup.py 192.168.1.100 --diff"
    )
    parser.add_argument("target", nargs="?", help="Device IP address")
    parser.add_argument("--batch", help="File with one IP per line")
    parser.add_argument("--port", type=int, default=23, help="SIS port (default 23)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--diff", action="store_true", help="Diff against last backup")
    parser.add_argument("--json", action="store_true", help="Output config as JSON to stdout")
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

    for host in targets:
        print(f"\n{'='*60}")
        print(f"Backing up: {host}")
        print(f"{'='*60}")

        config = collect_device_config(host, args.port, args.timeout)

        if args.json:
            print(json.dumps(config, indent=2))

        backup_path = save_backup(config, host)
        print(f"Backup saved: {backup_path}")

        if args.diff:
            latest = find_latest_backup(host)
            # Get second-most-recent (skip the one we just saved)
            prefix = host.replace(".", "_")
            all_files = sorted(BACKUP_DIR.glob(f"{prefix}-*.json"), reverse=True)
            if len(all_files) >= 2:
                diff_backups(config, all_files[1])
            else:
                print("No previous backup to compare against.")


if __name__ == "__main__":
    main()
