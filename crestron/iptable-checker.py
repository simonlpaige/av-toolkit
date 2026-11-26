#!/usr/bin/env python3
"""
crestron-iptable-checker.py — Verify Crestron processor IP table entries.

Crestron processors use an IP table to define connections to touch
panels, DM/NVX endpoints and other control systems. A dead entry —
pointing at a device that's offline, re-addressed, or on the wrong
network — is one of the most common causes of intermittent control
failures.

This script reads the IP table from the processor console (CTP,
TCP 41795), then reports:

  - Entries the processor itself shows as not ONLINE
  - Entries whose address doesn't answer a ping
  - Entries outside the subnet(s) you expect (--expect-subnet)
  - The same IP ID used more than once

Usage:
    python iptable-checker.py 192.168.1.10                 # single processor
    python iptable-checker.py --batch processors.txt       # batch from file
    python iptable-checker.py 192.168.1.10 --expect-subnet 192.168.1.0/24
    python iptable-checker.py --file iptable.txt           # check pasted console output

Authentication: CTP on 41795 is open on 2-/3-series unless hardened.
4-series and anything with authentication on only talks SSH — run
IPTABLE in your SSH client, save the output, and use --file. --user and
--password are tried if the console asks for a login.

Exit code is 0 only when every processor answered with no issues.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.net import Session, read_targets_file, run_parallel  # noqa: E402

PROMPT = r"[\w.\-]+>\s*\Z"
IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


class ConsoleError(Exception):
    pass


class CrestronConsole:
    """A CTP console session held open for several commands."""

    def __init__(self, host, port=41795, timeout=5.0, user=None, password=None):
        self.session = Session(host, port, timeout)
        try:
            self.session.send("\r")
            text, ok = self.session.read_until(PROMPT + r"|[Ll]ogin:\s*\Z|[Pp]assword:\s*\Z")
            if re.search(r"login:\s*\Z|password:\s*\Z", text, re.I):
                if user is None and password is None:
                    raise ConsoleError("console requires a login (use --user/--password, "
                                       "or run IPTABLE over SSH and use --file)")
                if re.search(r"login:\s*\Z", text, re.I):
                    self.session.send((user or "") + "\r")
                    self.session.read_until(r"[Pp]assword:\s*\Z")
                self.session.send((password or "") + "\r")
                text, ok = self.session.read_until(PROMPT)
                if not ok:
                    raise ConsoleError("login rejected")
            elif not ok:
                raise ConsoleError("no console prompt — is this a Crestron processor on CTP 41795?")
            self.prompt = text.strip().splitlines()[-1].strip()
        except Exception:
            self.session.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def run(self, command: str) -> str:
        self.session.send(command + "\r")
        text, _ = self.session.read_until(PROMPT)
        lines = text.replace("\r", "").split("\n")
        if lines and lines[0].strip().lower().endswith(command.lower()):
            lines = lines[1:]                      # echoed command
        if lines and re.fullmatch(PROMPT.replace(r"\Z", ""), lines[-1].strip() + " "):
            lines = lines[:-1]                     # trailing prompt
        return "\n".join(lines).strip()


def parse_iptable(output: str) -> list:
    """
    Parse IPTABLE console output into entries.

    Column order differs between firmware families, e.g.

        CIP_ID  Type    Status    DevID   Port   IP Address/SiteName
           3    Gway    ONLINE    1       41794  192.168.1.201
          1A    Gway    OFFLINE           41794  tsw-lobby.av.local

    so rows are read by what each token looks like, not where it sits.
    """
    entries = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or not re.fullmatch(r"[0-9A-Fa-f]{1,2}", tokens[0]):
            continue
        if not re.fullmatch(r"[A-Za-z_]+", tokens[1]) or not re.fullmatch(r"[A-Za-z_]+", tokens[2]):
            continue
        rest = tokens[3:]
        address = next((t for t in rest if IPV4.fullmatch(t)), None)
        if address is None:
            address = next((t for t in rest[:4]
                            if re.search(r"[A-Za-z]", t) and re.fullmatch(r"[\w.\-]+", t)
                            and not re.fullmatch(r"[0-9A-Fa-f]+", t)), "")
        numbers = [t for t in rest[:4] if t != address and re.fullmatch(r"[0-9A-Fa-f]+", t)]
        port = next((t for t in numbers if t.isdigit() and int(t) >= 1024), "")
        dev_id = next((t for t in numbers if t != port), "")
        tail = rest[rest.index(address) + 1:] if address in rest else []
        tail = [t for t in tail if t not in (port, dev_id)]
        entries.append({
            "ip_id": tokens[0].upper().zfill(2),
            "type": tokens[1],
            "status": tokens[2].upper(),
            "address": address,
            "port": port,
            "device_id": dev_id,
            "model": tail[0] if tail else "",
        })
    return entries


def ping_host(host: str, timeout: float = 2.0) -> bool:
    """One ICMP echo via the system ping. True if the host answers."""
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, int(timeout))), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout + 2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_issues(entries: list, expect_subnets=(), ping=ping_host) -> list:
    """Run every check against a parsed IP table."""
    issues = []
    pingable = [e["address"] for e in entries
                if e["address"] and e["address"] != "0.0.0.0" and not e["address"].startswith("127.")]
    alive = dict(zip(pingable, run_parallel(ping, pingable, 16))) if ping else {}

    for e in entries:
        tag = f"IP ID {e['ip_id']} ({e['type']})"
        addr = e["address"]
        if e["status"] != "ONLINE":
            issues.append(f"{tag} → {addr or 'no address'} — status {e['status']}")
        if addr in alive and not alive[addr]:
            issues.append(f"{tag} → {addr} — no ping reply")
        if expect_subnets and IPV4.fullmatch(addr or "") and not addr.startswith("127."):
            ip = ipaddress.IPv4Address(addr)
            if not any(ip in net for net in expect_subnets):
                nets = ", ".join(str(n) for n in expect_subnets)
                issues.append(f"{tag} → {addr} — outside expected subnet ({nets})")

    seen = {}
    for e in entries:
        seen.setdefault(e["ip_id"], []).append(e["address"] or "?")
    for ip_id, addrs in sorted(seen.items()):
        if len(addrs) > 1:
            issues.append(f"IP ID {ip_id} appears {len(addrs)} times ({', '.join(addrs)}) — "
                          "fine across program slots, a conflict within one")
    return issues


def check_processor(host: str, port: int = 41795, timeout: float = 5.0, user=None,
                    password=None, expect_subnets=(), ping=ping_host) -> dict:
    """Read and check one processor's IP table."""
    result = {"host": host, "reachable": False, "prompt": "", "firmware": "",
              "ip_table": [], "issues": []}
    try:
        with CrestronConsole(host, port, timeout, user, password) as console:
            result["reachable"] = True
            result["prompt"] = console.prompt.rstrip(">")
            version = console.run("VER")
            result["firmware"] = version.splitlines()[0].strip() if version else ""
            result["ip_table"] = parse_iptable(console.run("IPTABLE"))
    except (OSError, ConsoleError) as e:
        result["issues"].append(f"Cannot read processor: {e}")
        return result
    result["issues"] = find_issues(result["ip_table"], expect_subnets, ping)
    return result


def print_result(r: dict):
    print(f"\n{'=' * 60}\n{r['host']}" + (f"  [{r['prompt']}]" if r.get("prompt") else ""))
    print("=" * 60)
    if not r["reachable"]:
        print(f"  ✗ {r['issues'][0]}")
        return
    if r["firmware"]:
        print(f"  {r['firmware'][:74]}")
    print(f"  IP table entries: {len(r['ip_table'])}")
    for e in r["ip_table"]:
        icon = "✓" if e["status"] == "ONLINE" else "✗"
        print(f"    {icon} {e['ip_id']}  {e['type']:<6} {e['status']:<9} {e['address']:<24} {e['model']}")
    if r["issues"]:
        print(f"\n  Issues ({len(r['issues'])}):")
        for issue in r["issues"]:
            print(f"    ⚠ {issue}")
    elif r["ip_table"]:
        print("\n  ✓ All IP table entries verified.")
    else:
        print("\n  ⚠ IP table is empty (or the output format was not recognised).")


def main():
    parser = argparse.ArgumentParser(
        description="Verify Crestron processor IP table entries.",
        epilog="Example: python iptable-checker.py 192.168.1.10")
    parser.add_argument("target", nargs="?", help="Crestron processor IP")
    parser.add_argument("--batch", help="File with one processor IP per line")
    parser.add_argument("--file", help="Check saved IPTABLE console output instead of a live unit")
    parser.add_argument("--port", type=int, default=41795, help="CTP console port (default 41795)")
    parser.add_argument("--timeout", type=float, default=5.0, help="Connection timeout")
    parser.add_argument("--user", help="Console username, if authentication is on")
    parser.add_argument("--password", help="Console password, if authentication is on")
    parser.add_argument("--expect-subnet", action="append", default=[], metavar="CIDR",
                        help="Flag entries outside this subnet (repeatable)")
    parser.add_argument("--no-ping", action="store_true", help="Skip the ping check")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    try:
        subnets = [ipaddress.IPv4Network(s, strict=False) for s in args.expect_subnet]
    except ValueError as e:
        parser.error(f"--expect-subnet: {e}")
    ping = None if args.no_ping else ping_host

    results = []
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            parser.error(f"cannot read {args.file}: {e}")
        table = parse_iptable(text)
        results.append({"host": args.file, "reachable": True, "prompt": "", "firmware": "",
                        "ip_table": table, "issues": find_issues(table, subnets, ping)})
    else:
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
        for host in targets:
            results.append(check_processor(host, args.port, args.timeout, args.user,
                                           args.password, subnets, ping))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print_result(r)
    clean = all(r["reachable"] and r["ip_table"] and not r["issues"] for r in results)
    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
