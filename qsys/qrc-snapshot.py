#!/usr/bin/env python3
"""
qrc-snapshot.py — Snapshot and diff Q-SYS Cores from your laptop.

Talks to a Core over QRC (Q-SYS Remote Control, JSON-RPC on TCP 1710),
reads the value of every control on every named component, and saves it
or compares it. No plugin needed in the design.

  - Identical rooms that don't sound identical: diff Core against Core.
  - "Nothing changed, it just stopped working": diff today's Core
    against the snapshot you saved at handoff.

Usage:
    python qrc-snapshot.py 192.168.1.10                       # status + component summary
    python qrc-snapshot.py 192.168.1.10 --save room1.json     # save a snapshot
    python qrc-snapshot.py 192.168.1.10 192.168.1.11          # diff two Cores (first = reference)
    python qrc-snapshot.py room1.json 192.168.1.10            # diff a Core against a saved snapshot
    python qrc-snapshot.py room1.json room2.json --tolerance 0.5

Only components with a Code Name and Script Access set to "External" or
"All" are visible over QRC. Meters and other read-only controls are
recorded but left out of diffs unless you pass --include-read, since
they never sit still.

If the Core has access control enabled for External Control, pass
--user and --pin.

Exit code: 0 match / saved, 1 differences found, 2 a Core failed.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


class QRCError(Exception):
    pass


class QRC:
    """A QRC session: null-terminated JSON-RPC 2.0 over TCP."""

    def __init__(self, host: str, port: int = 1710, timeout: float = 5.0, user=None, pin=None):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._id = 0
        if user is not None:
            self.call("Logon", {"User": user, "Password": pin or ""})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _next_message(self, deadline: float) -> dict:
        while b"\x00" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise QRCError("timed out waiting for the Core to reply")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                raise QRCError("timed out waiting for the Core to reply") from None
            if not chunk:
                raise QRCError("the Core closed the connection")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\x00", 1)
        return json.loads(raw.decode("utf-8", errors="replace")) if raw.strip() else {}

    def call(self, method: str, params=None):
        """Call a QRC method and return its result. Unsolicited messages are skipped."""
        self._id += 1
        request = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params is not None:
            request["params"] = params
        self.sock.sendall(json.dumps(request).encode() + b"\x00")
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self._next_message(deadline)
            if msg.get("id") != self._id:
                continue                      # EngineStatus and other notifications
            if "error" in msg:
                err = msg["error"] or {}
                raise QRCError(f"{method}: {err.get('message', err)} (code {err.get('code', '?')})")
            return msg.get("result")


def take_snapshot(host: str, port: int = 1710, timeout: float = 5.0, user=None, pin=None) -> dict:
    """Read Core status and every control on every externally visible component."""
    with QRC(host, port, timeout, user, pin) as core:
        status = core.call("StatusGet", 0) or {}
        snapshot = {
            "host": host,
            "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "design": status.get("DesignName", ""),
            "design_code": status.get("DesignCode", ""),
            "platform": status.get("Platform", ""),
            "state": status.get("State", ""),
            "status": (status.get("Status") or {}).get("String", ""),
            "status_code": (status.get("Status") or {}).get("Code"),
            "components": {},
        }
        for comp in core.call("Component.GetComponents", "test") or []:
            name = comp.get("Name")
            if not name:
                continue
            reply = core.call("Component.GetControls", {"Name": name}) or {}
            controls = {}
            for c in reply.get("Controls", []):
                controls[c.get("Name", "")] = {
                    "value": c.get("Value"), "string": c.get("String", ""),
                    "direction": c.get("Direction", ""), "type": c.get("Type", ""),
                }
            snapshot["components"][name] = {"type": comp.get("Type", ""), "controls": controls}
    return snapshot


def load_snapshot(target: str, port: int, timeout: float, user, pin) -> dict:
    """A target is a saved snapshot (.json) or a live Core address."""
    if target.lower().endswith(".json"):
        data = json.loads(Path(target).read_text(encoding="utf-8"))
        if "components" not in data:
            raise ValueError(f"{target} is not a Q-SYS snapshot (no 'components')")
        data.setdefault("host", target)
        return data
    return take_snapshot(target, port, timeout, user, pin)


def diff_snapshots(ref: dict, target: dict, tolerance: float = 0.05, include_read=False) -> list:
    """Compare two snapshots; return a list of difference dicts."""
    diffs = []

    def add(kind, where, a, b):
        diffs.append({"kind": kind, "where": where, "reference": a, "target": b})

    if ref.get("design_code") and target.get("design_code") and ref["design_code"] != target["design_code"]:
        add("design", "design code (different compiled designs)",
            f"{ref.get('design')} [{ref['design_code']}]", f"{target.get('design')} [{target['design_code']}]")

    a_comps, b_comps = ref["components"], target["components"]
    for name in sorted(set(a_comps) | set(b_comps)):
        if name not in b_comps:
            add("missing", name, "present", "no such component")
            continue
        if name not in a_comps:
            add("extra", name, "no such component", "present")
            continue
        a_ctl, b_ctl = a_comps[name]["controls"], b_comps[name]["controls"]
        for cname in sorted(set(a_ctl) | set(b_ctl)):
            a, b = a_ctl.get(cname), b_ctl.get(cname)
            if a is None or b is None:
                add("control", f"{name} : {cname}", "present" if a else "absent", "present" if b else "absent")
                continue
            if not include_read and "Read" == (a.get("direction") or b.get("direction")):
                continue
            av, bv = a.get("value"), b.get("value")
            numeric = (isinstance(av, (int, float)) and isinstance(bv, (int, float))
                       and not isinstance(av, bool) and not isinstance(bv, bool))
            if numeric:
                if abs(av - bv) > tolerance:
                    add("value", f"{name} : {cname}", a.get("string") or av, b.get("string") or bv)
            elif av != bv or (av is None and a.get("string") != b.get("string")):
                add("value", f"{name} : {cname}", a.get("string") or av, b.get("string") or bv)
    return diffs


def describe(snap: dict) -> str:
    controls = sum(len(c["controls"]) for c in snap["components"].values())
    return (f"{snap.get('design') or '(no design)'} on {snap.get('platform') or '?'} — "
            f"{snap.get('state') or '?'}, status {snap.get('status') or '?'} — "
            f"{len(snap['components'])} components, {controls} controls")


def main():
    parser = argparse.ArgumentParser(
        description="Snapshot and diff Q-SYS Core control values over QRC.",
        epilog="Example: python qrc-snapshot.py room1.json 192.168.1.10")
    parser.add_argument("targets", nargs="+", help="Core IPs or saved snapshot .json files (first = reference)")
    parser.add_argument("--save", metavar="FILE", help="Save the first target's snapshot as JSON")
    parser.add_argument("--tolerance", type=float, default=0.05,
                        help="Ignore numeric differences up to this much (default 0.05)")
    parser.add_argument("--include-read", action="store_true", help="Also diff read-only controls (meters)")
    parser.add_argument("--port", type=int, default=1710, help="QRC port (default 1710)")
    parser.add_argument("--timeout", type=float, default=5.0, help="Reply timeout in seconds")
    parser.add_argument("--user", help="External-control username, if access control is on")
    parser.add_argument("--pin", help="PIN for --user")
    parser.add_argument("--json", action="store_true", help="Print differences as JSON on stdout")
    args = parser.parse_args()
    log = sys.stderr if args.json else sys.stdout

    def capture(target):
        snap = load_snapshot(target, args.port, args.timeout, args.user, args.pin)
        print(f"  {target}: {describe(snap)}", file=log)
        return snap

    print("Reference:", file=log)
    try:
        ref = capture(args.targets[0])
    except (OSError, QRCError, ValueError) as e:
        print(f"  ✗ {args.targets[0]}: {e}", file=sys.stderr)
        sys.exit(2)
    if not ref["components"]:
        print("  ⚠ No components visible. Give blocks a Code Name and set Script Access to "
              "External or All.", file=log)
    if ref.get("status_code") not in (None, 0):
        print(f"  ⚠ Core status is not OK: {ref.get('status')}", file=log)
    if args.save:
        Path(args.save).write_text(json.dumps(ref, indent=2), encoding="utf-8")
        print(f"  Snapshot saved: {args.save}", file=log)

    all_diffs, failed = {}, 0
    for target in args.targets[1:]:
        print("\nComparing:", file=log)
        try:
            snap = capture(target)
        except (OSError, QRCError, ValueError) as e:
            failed += 1
            print(f"  ✗ {target}: {e}", file=log)
            continue
        diffs = diff_snapshots(ref, snap, args.tolerance, args.include_read)
        all_diffs[target] = diffs
        if not diffs:
            print("  ✓ Matches reference.", file=log)
        else:
            print(f"  ✗ {len(diffs)} difference(s):", file=log)
            for d in diffs:
                print(f"    [{d['kind']}] {d['where']}: reference {d['reference']} → {d['target']}", file=log)

    if args.json:
        print(json.dumps(all_diffs, indent=2))
    sys.exit(2 if failed else 1 if any(all_diffs.values()) else 0)


if __name__ == "__main__":
    main()
