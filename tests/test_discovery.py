"""mDNS discovery, against a fake responder on 127.0.0.1, plus the third batch of BOM tools."""
import socket
import struct
import threading

import pytest
from conftest import EXAMPLES, load_script

from avlib.bom import Item, load_bom

mdns = load_script("general/mdns-discover.py")
companion = load_script("dtools/companion-check.py")
elevation = load_script("dtools/rack-elevation.py")
REV_A = EXAMPLES / "dtools-bom-rev-a.csv"


# ------------------------------------------------------------------ mDNS

def name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\x00"


def rr(owner, rtype, rdata):
    return owner + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


def txt(*pairs):
    return b"".join(bytes([len(p)]) + p.encode() for p in pairs)


def response(*records):
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)


@pytest.fixture
def responder():
    """A UDP socket that answers each query type with canned packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    answers, seen, stop = {}, [], threading.Event()

    def loop():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                return
            qtype = struct.unpack("!H", data[-4:-2])[0]
            seen.append(qtype)
            for packet in answers.get(qtype, []):
                sock.sendto(packet, addr)
    threading.Thread(target=loop, daemon=True).start()
    yield sock.getsockname(), answers, seen
    stop.set()
    sock.close()


def test_name_compression_and_bad_packets():
    packet = b"\x00" * 12 + name("core.local") + b"\x04amp1\xc0\x0c"
    assert mdns.read_name(packet, 12) == ("core.local", 24)
    assert mdns.read_name(packet, 24) == ("amp1.core.local", 31)
    with pytest.raises(ValueError, match="loop"):
        mdns.read_name(b"\x00" * 12 + b"\xc0\x0c", 12)
    assert mdns.parse_response(b"\x00\x01") == []
    assert mdns.parse_response(mdns.build_query(["_x._udp.local"])) == []       # a query, not an answer
    cut = response(rr(name("a.local"), 1, bytes([10, 0, 0, 1])), rr(name("b.local"), 1, bytes([10, 0, 0, 2])))
    assert mdns.parse_response(cut[:-3]) == [("a.local", 1, "10.0.0.1")]         # keeps what parsed


def test_discover_dante_devices_with_all_records_in_one_answer(responder):
    dest, answers, seen = responder
    arc = "_netaudio-arc._udp.local"
    chan = "_netaudio-chan._udp.local"
    answers[12] = [
        response(rr(name(arc), 12, name(f"Core-3A.{arc}")),
                 rr(name(f"Core-3A.{arc}"), 33, struct.pack("!HHH", 0, 0, 4440) + name("Core-3A.local")),
                 rr(name(f"Core-3A.{arc}"), 16, txt("mf=QSC", "model=Core 110f", "router_vers=4.2.1")),
                 rr(name("Core-3A.local"), 1, bytes([192, 168, 1, 20]))),
        response(rr(name(chan), 12, name(f"Mic 1@MXA-Ceiling.{chan}")),
                 rr(name(chan), 12, name(f"Mic 2@MXA-Ceiling.{chan}")),
                 rr(name(arc), 12, name(f"MXA-Ceiling.{arc}")),
                 rr(name(f"MXA-Ceiling.{arc}"), 33, struct.pack("!HHH", 0, 0, 4440) + name("MXA-Ceiling.local")),
                 rr(name("MXA-Ceiling.local"), 1, bytes([192, 168, 1, 9]))),
    ]
    devices = mdns.discover(wait=0.4, dest=dest)
    assert [(d["name"], d["addresses"], d["channels"]) for d in devices] == [
        ("MXA-Ceiling", ["192.168.1.9"], 2), ("Core-3A", ["192.168.1.20"], 0)]       # numeric IP order
    core = devices[1]
    assert core["host"] == "Core-3A.local" and core["services"]["_netaudio-arc._udp"] == 4440
    assert core["txt"] == {"mf": "QSC", "model": "Core 110f", "router_vers": "4.2.1"}
    assert seen.count(12) == 1 and 33 not in seen                    # nothing missing, no follow-up


def test_discover_follows_up_when_the_answer_is_only_a_pointer(responder):
    dest, answers, seen = responder
    arc = "_netaudio-arc._udp.local"
    inst = f"Amp-A.{arc}"
    answers[12] = [response(rr(name(arc), 12, name(inst)))]
    answers[33] = [response(rr(name(inst), 33, struct.pack("!HHH", 0, 0, 4440) + name("Amp-A.local")))]
    answers[16] = [response(rr(name(inst), 16, txt("mf=Audinate")))]
    answers[1] = [response(rr(name("Amp-A.local"), 1, bytes([10, 1, 2, 3])))]
    devices = mdns.discover(wait=0.3, dest=dest)
    assert [(d["name"], d["addresses"], d["txt"]) for d in devices] == [("Amp-A", ["10.1.2.3"], {"mf": "Audinate"})]
    assert {12, 33, 16, 1} <= set(seen)


def test_discover_silence_and_unrelated_answers(responder):
    dest, answers, _ = responder
    assert mdns.discover(wait=0.2, dest=dest) == []
    answers[12] = [response(rr(name("_printer._tcp.local"), 12, name("LaserJet._printer._tcp.local")))]
    assert mdns.discover(wait=0.2, dest=dest) == []


# ----------------------------------------------------- companion check

def bom(*rows):
    return [Item(row=n, manufacturer="X", model=m, description=d, category=c, location=loc, quantity=q)
            for n, (m, d, c, loc, q) in enumerate(rows, 2)]


def test_companion_check_on_the_example():
    found = companion.check_companions(load_bom(REV_A)[0])
    assert {(f["level"], f["rule"], f["location"]) for f in found} == {
        ("ERROR", "Displays need mounts", "Courtroom 3A"),
        ("ERROR", "Racks need power distribution", "Rack 1 - Courtroom 3A")}


def test_companion_mounts_are_counted_once_and_misfiled_parts_are_notes():
    items = bom(("QM85C", "85in display", "Displays", "Room A", 2),
                ("XSM1U", "Large display wall mount", "Mounts", "Room A", 1),
                ("QM55C", "55in display", "Displays", "Room B", 1),
                ("XSM1U", "Large display wall mount", "Mounts", "Warehouse", 1))
    found = {(f["level"], f["location"]): f for f in companion.check_companions(items)}
    # Room A has 2 displays and 1 mount; the warehouse mount covers it -> a note. Room B is then short.
    assert found[("NOTE", "Room A")]["have"] == 2
    assert found[("ERROR", "Room B")]["message"].endswith("1 short")
    assert len(found) == 2

    shared = bom(("QM85C", "85in display", "Displays", "R", 1), ("LS800", "Laser projector", "Projectors", "R", 1),
                 ("M1", "Universal mount", "Mounts", "R", 1), ("S1", "120in projection screen", "Screens", "R", 1))
    got = companion.check_companions(shared)
    assert [(f["rule"], f["level"]) for f in got] == [("Projectors need mounts", "ERROR")]


def test_companion_things_that_must_not_trigger():
    quiet = bom(("DP-6", "DisplayPort cable 6ft", "Cable", "R", 4),
                ("TSW-1070", "10in touch screen", "Control", "R", 2),
                ("RM-1", "Rack mount kit for Core", "Accessories", "R", 1),
                ("SB-1", "Powered soundbar speaker", "Speakers", "R", 1),
                ("DA-4", "HDMI distribution amplifier", "Video", "R", 1))
    assert companion.check_companions(quiet) == []
    lonely = bom(("AD-C6T", "Ceiling speaker", "Speakers", "R", 8))
    assert [f["rule"] for f in companion.check_companions(lonely)] == ["Speakers need amplification"]
    assert companion.check_companions(lonely + bom(("SPA4", "4-channel amplifier", "Amplifiers", "Rack", 1))) == []


def test_companion_custom_rules(tmp_path):
    rules = companion.validate_rules([{"when": ["*touch*"], "needs": ["*table kit*"]}])
    assert rules[0]["scope"] == "location" and rules[0]["mode"] == "each"
    items = bom(("TSW-1070", "10in touch screen", "Control", "R", 2), ("TTK", "Tabletop table kit", "Control", "R", 1))
    assert companion.check_companions(items, rules)[0]["message"].endswith("1 short")
    for bad in ({"a": 1}, [{"when": ["x"]}], [{"when": ["x"], "needs": ["y"], "mode": "some"}]):
        with pytest.raises(ValueError):
            companion.validate_rules(bad)


# -------------------------------------------------------- rack elevation

def test_rack_elevation_example():
    racks = {r["name"]: r for r in elevation.build_elevations(load_bom(REV_A)[0], only=["rack"])}
    r1 = racks["Rack 1 - Courtroom 3A"]
    assert (r1["height"], r1["used"], r1["free"], r1["overflow"]) == (45, 7, 38, False)     # "45RU rack"
    bottom_up = [(s["model"], s["u_low"], s["u_high"]) for s in reversed(r1["slots"])]
    assert bottom_up[0] == ("DTP CrossPoint 84 4K", 1, 2)            # heaviest (13 lb) at the bottom
    assert bottom_up[-1] == ("CP4N", 7, 7)                           # lightest on top
    assert [m for m, *_ in bottom_up].count("SPA4-100") == 2         # quantity 2 -> two slots
    assert racks["Rack 2 - Courtroom 3B"]["height"] is None
    assert "Courtroom 3A" not in racks                               # nothing rack-mounted there


def test_rack_elevation_options_and_overflow():
    items = [Item(row=1, model="A", location="R", rack_units=2, weight=5),
             Item(row=2, model="B", location="R", rack_units=1, weight=50),
             Item(row=3, model="C", location="R", rack_units=1)]
    heavy = elevation.build_elevations(items)[0]
    assert [s["model"] for s in heavy["slots"]] == ["C", "A", "B"]           # top -> bottom
    as_bom = elevation.build_elevations(items, order="bom")[0]
    assert [s["model"] for s in as_bom["slots"]] == ["A", "B", "C"]
    vented = elevation.build_elevations(items, vent_every=1, start_u=3)[0]
    assert [(s["label"], s["u_low"]) for s in reversed(vented["slots"])] == [
        ("B", 3), ("(vent / blank)", 4), ("A", 5), ("(vent / blank)", 7), ("C", 8)]
    assert vented["used"] == 8 and vented["equipment_ru"] == 4
    tight = elevation.build_elevations(items, rack_ru=3)[0]
    assert tight["overflow"] and tight["free"] == -1
    assert "OVERFLOW: needs 4 RU, rack is 3 RU" in elevation.render_text([tight])
    assert elevation.build_elevations([Item(row=1, model="Wallplate", location="R")]) == []


def test_discover_falls_back_to_the_sender_when_no_address_record_comes_back(responder):
    dest, answers, _ = responder
    arc = "_netaudio-arc._udp.local"
    answers[12] = [response(rr(name(arc), 12, name(f"Lost.{arc}")),
                            rr(name(f"Lost.{arc}"), 33, struct.pack("!HHH", 0, 0, 4440) + name("Lost.local")))]
    devices = mdns.discover(wait=0.2, dest=dest)
    assert [(d["name"], d["addresses"]) for d in devices] == [("Lost", ["127.0.0.1"])] and "_senders" not in devices[0]


def test_report_takes_companion_and_discovery_output(responder, tmp_path):
    import json
    import subprocess
    import sys
    from conftest import ROOT
    report = load_script("general/commissioning-report.py")
    out = subprocess.run([sys.executable, str(ROOT / "dtools/companion-check.py"), str(REV_A), "--json"],
                         capture_output=True, text=True)
    assert out.returncode == 1
    title, status, summary, lines = report.classify(json.loads(out.stdout))(json.loads(out.stdout))
    assert (title, status, summary) == ("BOM companion items", "FAIL", "2 missing, 0 note(s)")

    dest, answers, _ = responder
    arc = "_netaudio-arc._udp.local"
    answers[12] = [response(rr(name(arc), 12, name(f"Core.{arc}")),
                            rr(name(f"Core.{arc}"), 33, struct.pack("!HHH", 0, 0, 4440) + name("Core.local")),
                            rr(name(f"Core.{arc}"), 16, txt("mf=QSC")),
                            rr(name("Core.local"), 1, bytes([10, 0, 0, 9])))]
    found = json.loads(json.dumps(mdns.discover(wait=0.2, dest=dest)))
    title, status, summary, lines = report.classify(found)(found)
    assert (title, summary) == ("Dante / mDNS devices found", "1 device(s)") and "| Core | 10.0.0.9 | QSC |" in lines[2]
