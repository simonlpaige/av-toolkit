#!/usr/bin/env python3
"""
biamp-config-diff.py — Compare Biamp Tesira DSP configurations.

Useful when deploying identical systems across multiple rooms. Polls
two or more Tesira devices, extracts their DSP block configuration,
and reports differences. Catches the "Room 4 sounds different" problem
before a client notices.

Usage:
    python config-diff.py 192.168.1.50 192.168.1.51
    python config-diff.py --batch rooms.txt --reference room1

Approach:
    Polls each device for its block list and key parameters, then
    diffs structurally. Does not require access to the .tesira file —
    works from the live device.

Simon L. Paige — AV Toolkit
License: MIT
"""

import socket
import argparse
import json
import sys
from typing import Optional


def ttp_command(host: str, command: str, port: int = 23, timeout: float = 3.0) -> str:
    """Send a TTP command to a Tesira device."""
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
                if b"\r\n" in response:
                    break
            return response.decode("ascii", errors="replace").strip()
    except Exception as e:
        return f"ERROR: {e}"


def parse_ttp_response(response: str) -> str:
    """Clean TTP response."""
    if response.startswith("+OK"):
        val = response[3:].strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        return val
    return response


def get_device_snapshot(host: str, port: int, timeout: float) -> dict:
    """Collect a configuration snapshot from a Biamp Tesira device."""
    snapshot = {
        "host": host,
        "alias": "",
        "version": "",
        "blocks": {},
    }

    # Basic identity
    snapshot["alias"] = parse_ttp_response(ttp_command(host, "DEVICE get alias", port, timeout))
    snapshot["version"] = parse_ttp_response(ttp_command(host, "DEVICE get version", port, timeout))

    # DSP block queries — these vary by firmware version
    # Common blocks to check
    block_queries = [
        "MIXER get numInputs 1",
        "MIXER get numOutputs 1",
        "MIXER get inputMute 1 1",
        "MIXER get inputGain 1 1",
        "MIXER get outputMute 1 1",
        "MIXER get outputGain 1 1",
        "EQ get type 1",
        "EQ get numBands 1",
        "LEVEL get gain 1",
        "LEVEL get mute 1",
        "ROUTER get numInputs 1",
        "ROUTER get numOutputs 1",
        "DELAY get delayTime 1",
        "AGC get enabled 1",
        "AGC get threshold 1",
    ]

    for query in block_queries:
        resp = ttp_command(host, query, port, timeout)
        snapshot["blocks"][query] = parse_ttp_response(resp)

    return snapshot


def diff_snapshots(ref: dict, target: dict) -> list:
    """Compare two device snapshots, return list of differences."""
    diffs = []

    # Check firmware match first
    if ref["version"] != target["version"]:
        diffs.append({
            "severity": "warning",
            "param": "firmware_version",
            "reference": ref["version"],
            "target": target["version"],
            "host": target["host"],
        })

    # Compare all block parameters
    for query in sorted(ref["blocks"]):
        ref_val = ref["blocks"].get(query, "N/A")
        tgt_val = target["blocks"].get(query, "N/A")

        if ref_val != tgt_val:
            diffs.append({
                "severity": "mismatch",
                "param": query,
                "reference": ref_val,
                "target": tgt_val,
                "host": target["host"],
            })

    return diffs


def main():
    parser = argparse.ArgumentParser(
        description="Compare Biamp Tesira DSP configurations between devices.",
        epilog="Example: python config-diff.py 192.168.1.50 192.168.1.51"
    )
    parser.add_argument("targets", nargs="*", help="Device IPs (first is reference)")
    parser.add_argument("--batch", help="File with one IP per line (first = reference)")
    parser.add_argument("--reference", help="Reference device IP (use with --batch)")
    parser.add_argument("--port", type=int, default=23, help="Tesira TCP port")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout")
    parser.add_argument("--json", action="store_true", help="Output diffs as JSON")
    args = parser.parse_args()

    targets = []
    if args.batch:
        with open(args.batch) as f:
            targets = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        if args.reference:
            # Move reference to front
            if args.reference in targets:
                targets.remove(args.reference)
            targets.insert(0, args.reference)
    elif args.targets:
        targets = args.targets
    else:
        parser.print_help()
        sys.exit(1)

    if len(targets) < 2:
        print("Need at least 2 devices to compare (reference + target).", file=sys.stderr)
        sys.exit(1)

    print(f"Reference device: {targets[0]}")
    print(f"Target devices:   {', '.join(targets[1:])}")
    print()

    # Capture reference snapshot
    print(f"Capturing reference: {targets[0]} ...")
    ref_snapshot = get_device_snapshot(targets[0], args.port, args.timeout)
    print(f"  Alias: {ref_snapshot['alias']}, FW: {ref_snapshot['version']}")

    # Compare each target
    all_diffs = {}
    for target in targets[1:]:
        print(f"\nComparing: {target} ...")
        tgt_snapshot = get_device_snapshot(target, args.port, args.timeout)
        print(f"  Alias: {tgt_snapshot['alias']}, FW: {tgt_snapshot['version']}")

        diffs = diff_snapshots(ref_snapshot, tgt_snapshot)
        all_diffs[target] = diffs

        if not diffs:
            print(f"  ✓ Configuration matches reference.")
        else:
            print(f"  ✗ {len(diffs)} difference(s) found:")
            for d in diffs:
                print(f"    [{d['severity']}] {d['param']}")
                print(f"      Reference: {d['reference']}")
                print(f"      Target:    {d['target']}")

    if args.json:
        print(json.dumps(all_diffs, indent=2))


if __name__ == "__main__":
    main()
