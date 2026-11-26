#!/usr/bin/env python3
"""
ip-schedule.py — Build an IP address schedule from the BOM, then prove it on site.

  build   Take a D-Tools BOM export (or a Vectorworks device list), pick
          out the networked devices, expand quantities to one row per
          unit, and assign addresses from a subnet. The result is a CSV
          for the IT department, the programmer, and the rack labels.

  verify  Read a schedule back and check it: duplicate addresses,
          duplicate names, addresses outside their subnet, and (unless
          --offline) whether each device answers on its control port.

Usage:
    python ip-schedule.py build project-bom.xlsx --subnet 192.168.10.0/24 --out ip-schedule.csv
    python ip-schedule.py build bom.csv --subnet 10.20.30.0/24 --start 50 --gateway 10.20.30.1
    python ip-schedule.py build bom.csv --subnet 10.1.0.0/24 --include "*camera*" --exclude "*speaker*"
    python ip-schedule.py verify ip-schedule.csv
    python ip-schedule.py verify ip-schedule.csv --offline

Which BOM lines are "networked"?
    The BOM doesn't say, so build guesses from category, description and
    model (processor, DSP, switch, touch panel, camera, display, codec,
    encoder/decoder, amplifier, Dante...). The guess is printed. Correct
    it with --include / --exclude patterns (wildcards, matched against
    model, category and description), or take every line with --all.
    Then open the CSV and fix what is left — it is a starting point.

Addresses are assigned in order of location, then manufacturer, then
model, starting at --start (default .10) and skipping the gateway.

The schedule CSV has Name, IP Address and Port columns, so it can be
fed straight to general/device-monitor.py.

Exit code (verify): 0 clean, 1 problems found, 2 could not read the file.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import csv
import fnmatch
import ipaddress
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avlib.bom import PLACEHOLDER_MODELS, add_table_args, load_bom  # noqa: E402
from avlib.net import run_parallel, tcp_open  # noqa: E402
from avlib.tabular import TableError, load_table, norm_key, parse_map_args, to_number  # noqa: E402

NETWORKED_RX = re.compile(
    r"processor|\bdsp\b|\bcore\b|switch|router|touch\s*(panel|screen)|\bpanel\b|camera|\bptz\b|display|"
    r"monitor|projector|codec|encoder|decoder|transmitter|receiver|amplifier|\bamp\b|dante|network|"
    r"controller|control|gateway|streaming|recorder|\bnvx\b|\bavoip\b|access point|\bwap\b|microphone array|"
    r"ceiling array|video bar|signage|media player|\bpdu\b|\bups\b", re.I)
NOT_NETWORKED_RX = re.compile(
    r"cable|connector|mount|bracket|rack\b|shelf|blank|plate|speaker|loudspeaker|license|labor|"
    r"faceplate|adapter|\bvent\b|screws?|hardware|case\b", re.I)

# Control port to check, by manufacturer. Anything else falls back to 80.
CONTROL_PORTS = [("qsc", 1710), ("q-sys", 1710), ("qsys", 1710), ("crestron", 41794), ("extron", 23),
                 ("biamp", 23), ("shure", 2202), ("amx", 1319), ("lightware", 6107), ("kramer", 5000)]

COLUMNS = ["Name", "Manufacturer", "Model", "Location", "IP Address", "Subnet Mask", "Gateway",
           "Port", "MAC Address", "Notes"]

SCHEDULE_ALIASES = {
    "name": ["device", "device name", "hostname", "host name", "component id", "tag"],
    "ip": ["ip address", "address", "ip addr", "ipv4", "ipv4 address"],
    "mask": ["subnet mask", "subnet", "netmask"],
    "gateway": ["default gateway", "gw"],
    "port": ["tcp port", "control port"],
    "location": ["room", "location name", "area"],
}


def is_networked(item, include=(), exclude=(), take_all=False) -> bool:
    fields = [f for f in (item.model, item.category, item.description) if f]

    def hit(patterns):
        return any(fnmatch.fnmatch(f.lower(), p.lower()) for p in patterns for f in fields)

    if norm_key(item.model) in PLACEHOLDER_MODELS or hit(exclude):
        return False
    if take_all or hit(include):
        return True
    text = " ".join([item.category, item.description])
    if NOT_NETWORKED_RX.search(text):
        return False
    return bool(NETWORKED_RX.search(text))


def control_port(manufacturer: str, model: str = "") -> int:
    lower = manufacturer.lower()
    port = next((port for name, port in CONTROL_PORTS if name in lower), 80)
    if port == 1710 and "core" not in model.lower():
        return 80                      # only Cores speak QRC; amps and peripherals have a web UI
    return port


def build_schedule(items, subnet, start=10, gateway=None, include=(), exclude=(), take_all=False):
    """Return (rows, left_out). Raises ValueError if the subnet is too small."""
    net = ipaddress.IPv4Network(subnet, strict=False)
    hosts = list(net.hosts())
    gateway = ipaddress.IPv4Address(gateway) if gateway else hosts[0]
    pool = [h for h in hosts if int(h) - int(net.network_address) >= start and h != gateway]

    chosen = [it for it in items if is_networked(it, include, exclude, take_all)]
    left_out = [it for it in items if it not in chosen]
    chosen.sort(key=lambda it: (it.location.lower(), it.manufacturer.lower(), it.model.lower()))

    rows, used_names = [], {}
    for it in chosen:
        count = max(1, int(round(it.quantity)))
        for n in range(1, count + 1):
            if not pool:
                raise ValueError(f"{net} has no addresses left from .{start} up — "
                                 f"{sum(max(1, int(round(i.quantity))) for i in chosen)} needed")
            base = it.component_id if (it.component_id and count == 1) else (
                re.sub(r"[^A-Za-z0-9]+", "-", it.model).strip("-"))
            used_names[base] = used_names.get(base, 0) + 1
            name = base if (it.component_id and count == 1 and used_names[base] == 1) \
                else f"{base}-{used_names[base]:02d}"
            rows.append({"Name": name, "Manufacturer": it.manufacturer, "Model": it.model,
                         "Location": it.location, "IP Address": str(pool.pop(0)),
                         "Subnet Mask": str(net.netmask), "Gateway": str(gateway),
                         "Port": control_port(it.manufacturer, it.model), "MAC Address": "", "Notes": ""})
    return rows, left_out


def verify_schedule(rows, timeout=2.0, offline=False, probe=tcp_open) -> list:
    """rows: dicts with name, ip, mask, gateway, port. Returns [(level, name, message)]."""
    findings, by_ip, by_name, live = [], {}, {}, []
    for r in rows:
        name, ip_text = r.get("name") or "(unnamed)", (r.get("ip") or "").strip()
        if not ip_text:
            findings.append(("WARNING", name, "no IP address assigned"))
            continue
        try:
            ip = ipaddress.IPv4Address(ip_text)
        except ipaddress.AddressValueError:
            findings.append(("ERROR", name, f"'{ip_text}' is not a valid IPv4 address"))
            continue
        by_ip.setdefault(ip, []).append(name)
        if r.get("name"):
            by_name.setdefault(r["name"].lower(), [r["name"]]).append(ip_text)
        if r.get("mask"):
            try:
                net = ipaddress.IPv4Network(f"{ip}/{r['mask']}", strict=False)
                if ip in (net.network_address, net.broadcast_address) and net.prefixlen < 31:
                    findings.append(("ERROR", name, f"{ip} is the network or broadcast address of {net}"))
                if r.get("gateway"):
                    gw = ipaddress.IPv4Address(r["gateway"].strip())
                    if gw not in net:
                        findings.append(("ERROR", name, f"gateway {gw} is not in {net}"))
                    elif gw == ip:
                        findings.append(("ERROR", name, f"{ip} is the same as its gateway"))
            except ValueError:
                findings.append(("ERROR", name, f"bad subnet mask or gateway ({r['mask']}, {r.get('gateway')})"))
        live.append((name, ip_text, int(to_number(r.get("port"), 80) or 80)))

    for ip, names in by_ip.items():
        if len(names) > 1:
            findings.append(("ERROR", names[0], f"{ip} is assigned {len(names)} times: {', '.join(names)}"))
    for name, *ips in by_name.values():
        if len(ips) > 1:
            findings.append(("WARNING", name, f"name used {len(ips)} times ({', '.join(ips)})"))

    if not offline:
        results = run_parallel(lambda d: probe(d[1], d[2], timeout), live, 32)
        for (name, ip, port), ok in zip(live, results, strict=True):
            if not ok:
                findings.append(("ERROR", name, f"{ip} does not answer on port {port}"))
    order = {"ERROR": 0, "WARNING": 1}
    return sorted(findings, key=lambda f: order[f[0]])


def cmd_build(args, parser):
    try:
        items, _ = load_bom(args.bom, parse_map_args(args.map), args.sheet)
        rows, left_out = build_schedule(items, args.subnet, args.start, args.gateway,
                                        args.include, args.exclude, args.all)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        parser.error(str(e))
    handle = open(args.out, "w", newline="", encoding="utf-8-sig") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(handle, COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.out:
            handle.close()
    print(f"{len(rows)} address(es) assigned in {args.subnet}"
          + (f" → {args.out}" if args.out else ""), file=sys.stderr)
    if left_out and not args.all:
        names = sorted({it.label for it in left_out})
        print(f"Left out as not networked ({len(names)}) — use --include to add any back:", file=sys.stderr)
        for name in names:
            print(f"  {name}", file=sys.stderr)
    if not rows:
        sys.exit(1)


def cmd_verify(args, parser):
    try:
        table = load_table(args.schedule, SCHEDULE_ALIASES, required=["ip"],
                           overrides=parse_map_args(args.map), sheet=args.sheet)
    except TableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    findings = verify_schedule(table.rows, args.timeout, args.offline)
    print(f"IP SCHEDULE CHECK — {table.path.name} ({len(table.rows)} devices"
          f"{', offline' if args.offline else ''})")
    for level, name, message in findings:
        print(f"  {level:<8} {name[:28]:<29} {message}")
    errors = sum(1 for f in findings if f[0] == "ERROR")
    print(f"\n  {errors} error(s), {len(findings) - errors} warning(s)" if findings
          else "  ✓ No problems found.")
    sys.exit(1 if errors else 0)


def main():
    parser = argparse.ArgumentParser(description="Build and verify AV IP address schedules.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Assign addresses to the networked devices in a BOM")
    b.add_argument("bom", help="BOM export or device list (.csv, .tsv, .xlsx)")
    b.add_argument("--subnet", required=True, help="Subnet to assign from, e.g. 192.168.10.0/24")
    b.add_argument("--start", type=int, default=10, help="First host number to use (default 10)")
    b.add_argument("--gateway", help="Gateway address (default: first host in the subnet)")
    b.add_argument("--include", action="append", default=[], metavar="PATTERN",
                   help="Also treat matching lines as networked (repeatable)")
    b.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="Never treat matching lines as networked (repeatable)")
    b.add_argument("--all", action="store_true", help="Every line gets an address")
    b.add_argument("--out", help="Write the schedule to this file instead of stdout")
    add_table_args(b)
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="Check a schedule for conflicts and reachability")
    v.add_argument("schedule", help="IP schedule (.csv, .tsv, .xlsx)")
    v.add_argument("--offline", action="store_true", help="Check the data only; touch nothing on the network")
    v.add_argument("--timeout", type=float, default=2.0, help="Per-device TCP timeout")
    add_table_args(v)
    v.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args, parser)


if __name__ == "__main__":
    main()
