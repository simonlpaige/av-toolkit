#!/usr/bin/env python3
"""
mdns-discover.py — Find Dante (and other mDNS-advertised) devices on the LAN.

Dante devices don't answer a TCP port scan in any useful way: they
announce themselves over multicast DNS. This sends the same query Dante
Controller does and lists what answers — device name, IP address, and
the model / firmware details each device publishes about itself.

Usage:
    python mdns-discover.py                        # Dante devices
    python mdns-discover.py --wait 5               # listen longer on a busy network
    python mdns-discover.py --interface 192.168.1.50   # pick the NIC on a multi-homed laptop
    python mdns-discover.py --all                  # also NDI, AirPlay, Chromecast, web UIs, SSH
    python mdns-discover.py --service _ndi._tcp    # any service type
    python mdns-discover.py --json > dante.json

Notes:
    - mDNS does not cross routers. Run this from a machine on the same
      VLAN as the devices, and if the laptop has several network
      adapters, name the right one with --interface.
    - A managed switch with IGMP snooping and no querier, or a client
      firewall blocking UDP 5353, will make devices invisible. If Dante
      Controller can't see them from this machine either, look there.
    - Devices in a different subnet from the one they are plugged into
      still answer, which makes this a quick way to find a Dante device
      with a wrong static IP.

Dante service types queried by default:
    _netaudio-arc._udp   routing/control (one per device — the device list)
    _netaudio-cmc._udp   clock and device management
    _netaudio-dbc._udp   device browsing / configuration
    _netaudio-chan._udp  one per advertised transmit channel

Exit code: 0 devices found, 1 nothing answered.

Simon L. Paige — AV Toolkit
License: MIT
"""

import argparse
import ipaddress
import json
import socket
import struct
import sys
import time

MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353
TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33

DANTE_SERVICES = ["_netaudio-arc._udp", "_netaudio-cmc._udp", "_netaudio-dbc._udp", "_netaudio-chan._udp"]
OTHER_AV_SERVICES = ["_ndi._tcp", "_airplay._tcp", "_raop._tcp", "_googlecast._tcp", "_http._tcp",
                     "_https._tcp", "_ssh._tcp"]


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(names, qtype=TYPE_PTR) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, len(names), 0, 0, 0)
    return header + b"".join(encode_name(n) + struct.pack("!HH", qtype, 1) for n in names)


def read_name(packet: bytes, offset: int) -> tuple:
    """Decode a possibly compressed DNS name. Returns (name, offset after it)."""
    labels, end, hops = [], None, 0
    while True:
        if offset >= len(packet):
            raise ValueError("truncated name")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | packet[offset + 1]
            hops += 1
            if hops > 32:
                raise ValueError("compression loop")
        elif length == 0:
            offset += 1
            break
        else:
            labels.append(packet[offset + 1:offset + 1 + length].decode("utf-8", errors="replace"))
            offset += 1 + length
    return ".".join(labels), (end if end is not None else offset)


def parse_response(packet: bytes) -> list:
    """Return the resource records in a DNS message as (name, type, data) tuples."""
    if len(packet) < 12:
        return []
    _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", packet[:12])
    if not flags & 0x8000:
        return []                                   # a query, not a response
    records, offset = [], 12
    try:
        for _ in range(qd):
            _, offset = read_name(packet, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = read_name(packet, offset)
            rtype, _, _, rdlen = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            rdata = packet[offset:offset + rdlen]
            if rtype == TYPE_PTR:
                records.append((name, rtype, read_name(packet, offset)[0]))
            elif rtype == TYPE_SRV and rdlen >= 7:
                port = struct.unpack("!H", rdata[4:6])[0]
                records.append((name, rtype, (read_name(packet, offset + 6)[0], port)))
            elif rtype == TYPE_A and len(rdata) == 4:
                records.append((name, rtype, socket.inet_ntoa(rdata)))
            elif rtype == TYPE_TXT:
                pairs, i = {}, 0
                while i < len(rdata):
                    n = rdata[i]
                    entry = rdata[i + 1:i + 1 + n].decode("utf-8", errors="replace")
                    i += 1 + n
                    if entry:
                        key, _, value = entry.partition("=")
                        pairs[key] = value
                records.append((name, rtype, pairs))
            offset += rdlen
    except (ValueError, struct.error, IndexError, OSError):
        pass                                        # keep what parsed before the damage
    return records


def _open_socket(interface=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    if interface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sock.bind((interface, 0))
    else:
        sock.bind(("", 0))
    return sock


def _exchange(sock, packet, dest, wait) -> list:
    """Send one query, collect (records, sender ip) until `wait` seconds pass."""
    sock.sendto(packet, dest)
    got, deadline = [], time.monotonic() + wait
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return got
        sock.settimeout(left)
        try:
            data, addr = sock.recvfrom(9000)
        except (socket.timeout, TimeoutError):
            return got
        except OSError:
            return got
        got.append((parse_response(data), addr[0]))


def discover(services=None, wait=3.0, interface=None, dest=(MDNS_GROUP, MDNS_PORT)) -> list:
    """
    Browse mDNS for the given service types. Returns a list of devices:
    {name, host, addresses, services: {type: port}, txt: {key: value}}.
    """
    services = [s.strip(".") for s in (services or DANTE_SERVICES)]
    instances, hosts, senders = {}, {}, {}

    def absorb(batches):
        for records, sender in batches:
            for name, rtype, data in records:
                key = name.lower()
                if rtype == TYPE_PTR:
                    svc = next((s for s in services if key == f"{s}.local".lower()), None)
                    if svc:
                        inst = instances.setdefault(data.lower(), {"instance": data, "type": svc})
                        senders.setdefault(data.lower(), sender)
                elif rtype == TYPE_SRV:
                    inst = instances.setdefault(key, {"instance": name, "type": _service_of(name, services)})
                    inst["host"], inst["port"] = data
                    senders.setdefault(key, sender)
                elif rtype == TYPE_TXT:
                    inst = instances.setdefault(key, {"instance": name, "type": _service_of(name, services)})
                    inst.setdefault("txt", {}).update(data)
                elif rtype == TYPE_A:
                    hosts.setdefault(key, [])
                    if data not in hosts[key]:
                        hosts[key].append(data)

    sock = _open_socket(interface)
    try:
        absorb(_exchange(sock, build_query([f"{s}.local" for s in services]), dest, wait))
        # Responders usually attach SRV/TXT/A records, but not always: ask for what is missing.
        need = [i["instance"] for i in instances.values()
                if "host" not in i and i.get("type") and not i["type"].endswith("-chan._udp")]
        if need:
            follow = min(wait, 1.5)
            absorb(_exchange(sock, build_query(need, TYPE_SRV), dest, follow))
            absorb(_exchange(sock, build_query(need, TYPE_TXT), dest, follow / 2))
        missing = sorted({i["host"] for i in instances.values()
                          if i.get("host") and i["host"].lower() not in hosts})
        if missing:
            absorb(_exchange(sock, build_query(missing, TYPE_A), dest, min(wait, 1.5)))
    finally:
        sock.close()

    devices = {}
    for key, inst in instances.items():
        if not inst.get("type"):
            continue
        name = inst["instance"][: -len(f".{inst['type']}.local")] if inst["instance"].lower().endswith(
            f".{inst['type']}.local".lower()) else inst["instance"]
        # Channel adverts are "channel@device"; fold them into their device.
        device_name = name.split("@", 1)[1] if (inst["type"].endswith("-chan._udp") and "@" in name) else name
        host = inst.get("host", "")
        dev = devices.setdefault(device_name.lower(), {"name": device_name, "host": "", "addresses": [],
                                                       "services": {}, "txt": {}, "channels": 0,
                                                       "_senders": []})
        if key in senders and senders[key] not in dev["_senders"]:
            dev["_senders"].append(senders[key])
        dev["host"] = dev["host"] or host.rstrip(".")
        for ip in hosts.get(host.lower(), []):
            if ip not in dev["addresses"]:
                dev["addresses"].append(ip)
        if inst["type"].endswith("-chan._udp"):
            dev["channels"] += 1
        else:
            dev["txt"].update(inst.get("txt", {}))
        dev["services"][inst["type"]] = inst.get("port", dev["services"].get(inst["type"]))
    for dev in devices.values():
        if not dev["addresses"]:                  # no A record came back: fall back to who answered
            dev["addresses"] = sorted(dev["_senders"])
        del dev["_senders"]

    def sort_key(d):
        try:
            return (0, int(ipaddress.IPv4Address(d["addresses"][0])), d["name"].lower())
        except (IndexError, ValueError):
            return (1, 0, d["name"].lower())
    return sorted(devices.values(), key=sort_key)


def _service_of(instance_name, services):
    lower = instance_name.lower()
    return next((s for s in services if lower.endswith(f".{s}.local".lower())), None)


def main():
    parser = argparse.ArgumentParser(
        description="Discover Dante and other mDNS-advertised AV devices on the local network.",
        epilog="Example: python mdns-discover.py --interface 192.168.1.50 --wait 5")
    parser.add_argument("--service", action="append", default=[], metavar="TYPE",
                        help="Service type to browse, e.g. _ndi._tcp (repeatable; replaces the Dante set)")
    parser.add_argument("--all", action="store_true",
                        help="Dante plus NDI, AirPlay, Chromecast, HTTP(S) and SSH adverts")
    parser.add_argument("--wait", type=float, default=3.0, help="Seconds to listen for answers (default 3)")
    parser.add_argument("--interface", metavar="IP", help="Local IP of the network adapter to send from")
    parser.add_argument("--txt", action="store_true", help="Show every TXT record, not just the summary")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    services = args.service or (DANTE_SERVICES + OTHER_AV_SERVICES if args.all else DANTE_SERVICES)
    if args.interface:
        try:
            ipaddress.IPv4Address(args.interface)
        except ValueError:
            parser.error(f"--interface wants this machine's IP on that adapter, not '{args.interface}'")
    try:
        devices = discover(services, args.wait, args.interface)
    except OSError as e:
        print(f"Error: could not send the mDNS query ({e}). Check --interface and that the adapter is up.",
              file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(devices, indent=2))
    elif not devices:
        print("No devices answered. mDNS does not cross routers — are you on the same VLAN, and on the "
              "right adapter (--interface)?", file=sys.stderr)
    else:
        print(f"Found {len(devices)} device(s):\n")
        print(f"  {'Name':<32}{'IP':<17}{'Details'}")
        print("  " + "-" * 86)
        for d in devices:
            txt = d["txt"]
            bits = [txt[k] for k in ("mf", "model", "router_info") if txt.get(k)]
            bits += [f"fw {txt[k]}" for k in ("router_vers", "server_vers") if txt.get(k)][:1]
            if d["channels"]:
                bits.append(f"{d['channels']} tx ch")
            kinds = sorted(s.split(".")[0].lstrip("_") for s in d["services"]
                           if not s.startswith("_netaudio"))
            bits += kinds
            print(f"  {d['name'][:31]:<32}{', '.join(d['addresses']) or '?':<17}{' · '.join(bits)}")
            if args.txt:
                for k in sorted(txt):
                    print(f"      {k} = {txt[k]}")
    sys.exit(0 if devices else 1)


if __name__ == "__main__":
    main()
