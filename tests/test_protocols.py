"""Control-protocol scripts, exercised against fake devices on 127.0.0.1."""
import json
import re
import time

import pytest
from conftest import closed_port, load_script

from avlib import net
from avlib.sis import ESC, SISDevice, decode_command, describe_error
from avlib.tesira import Tesira, TesiraError, fault_names, parse_value

scanner = load_script("extron/device-scanner.py")
backup = load_script("extron/config-backup.py")
tstatus = load_script("biamp/tesira-status.py")
tdiff = load_script("biamp/config-diff.py")
iptable = load_script("crestron/iptable-checker.py")
qrc = load_script("qsys/qrc-snapshot.py")
monitor = load_script("general/device-monitor.py")

IAC = b"\xff"


# ------------------------------------------------------------------- net

def test_expand_targets():
    assert net.expand_targets("192.168.1.0/30") == ["192.168.1.1", "192.168.1.2"]
    assert net.expand_targets("10.0.0.4/31") == ["10.0.0.4", "10.0.0.5"]
    assert net.expand_targets("10.0.0.254-10.0.1.1") == ["10.0.0.254", "10.0.0.255", "10.0.1.0", "10.0.1.1"]
    assert net.expand_targets("10.0.0.9-11") == ["10.0.0.9", "10.0.0.10", "10.0.0.11"]
    assert net.expand_targets("core-1.av.local") == ["core-1.av.local"]
    for bad in ("10.0.0.9-3", "10.0.0.0/40", "300.1.1.1/24", "10.0.0.0/8", ""):
        with pytest.raises(net.TargetError):
            net.expand_targets(bad)


def test_read_targets_file(tmp_path):
    f = tmp_path / "rooms.txt"
    f.write_text("# rooms\n192.168.1.50   Courtroom 3A DSP\n\n192.168.1.51, Room B # twin\n10.0.0.1\n")
    assert net.read_targets_file(f) == [("192.168.1.50", "Courtroom 3A DSP"),
                                        ("192.168.1.51", "Room B"), ("10.0.0.1", "")]


# ---------------------------------------------------------------- Extron

def fake_extron(password=None, banner=True, state=None, sent=None):
    replies = {"I": "V8X4 A8X4", "N": "60-1381-01", "Q": "1.07", "*Q": "1.07.0003", "1I": "DTP CrossPoint 84",
               "2I": "8x4 matrix", ESC + "CN\r": "Courtroom-3A-MTX", ESC + "CH\r": "00-05-A6-01-02-03",
               ESC + "CI\r": "192.168.1.20", ESC + "CS\r": "255.255.255.0", ESC + "CG\r": "192.168.1.1",
               ESC + "DH\r": "0", "V": "Vol-20", "Z": "E10", "!": ""}
    replies.update(state or {})

    def handler(sock):
        if banner:
            sock.sendall(b"\r\n(c) Copyright 2019, Extron Electronics, DTP CrossPoint 84, V1.07, 60-1381-01\r\n"
                         b"Tue, 12 Feb 2019 13:46:08\r\n")
        if password is not None:
            sock.sendall(b"\r\nPassword:")
            got = b""
            while not got.endswith(b"\r"):
                got += sock.recv(64)
            if got[:-1].decode() != password:
                sock.sendall(b"\r\nPassword:")
                return
            sock.sendall(b"\r\nLogin Administrator\r\n")
        buf = ""
        while True:
            data = sock.recv(256)
            if not data:
                return
            buf += data.decode("latin-1")
            while buf:
                cmd = next((c for c in sorted(replies, key=len, reverse=True) if buf.startswith(c)), None)
                if cmd is None:
                    if buf.startswith(ESC) and "\r" not in buf:
                        break
                    cmd, reply = buf[0], "E10"
                else:
                    reply = replies[cmd]
                buf = buf[len(cmd):]
                if sent is not None:
                    sent.append(cmd)
                if reply:
                    sock.sendall(reply.encode() + b"\r\n")
    return handler


def test_extron_scan_is_passive_when_the_banner_identifies_the_unit(serve):
    sent = []
    port = serve(fake_extron(sent=sent))
    dev = scanner.probe_host("127.0.0.1", port, 1.0)
    assert (dev.model, dev.firmware, dev.part_number) == ("DTP CrossPoint 84", "1.07", "60-1381-01")
    assert dev.name == "Courtroom-3A-MTX" and not dev.locked
    assert sent == [ESC + "CN\r"]


def test_extron_scan_no_banner_falls_back_to_queries(serve):
    port = serve(fake_extron(banner=False))
    dev = scanner.probe_host("127.0.0.1", port, 1.0)
    assert (dev.model, dev.firmware, dev.part_number) == ("DTP CrossPoint 84", "1.07", "60-1381-01")


def test_extron_scan_locked_unit_and_password(serve):
    port = serve(fake_extron(password="extron"))
    locked = scanner.probe_host("127.0.0.1", port, 1.0)
    assert locked.locked and locked.model == "DTP CrossPoint 84" and locked.name == ""
    opened = scanner.probe_host("127.0.0.1", port, 1.0, password="extron")
    assert not opened.locked and opened.name == "Courtroom-3A-MTX"
    assert scanner.probe_host("127.0.0.1", port, 1.0, password="wrong").locked


def test_extron_scan_ignores_other_telnet_services_and_dead_hosts(serve):
    port = serve(lambda s: (s.sendall(b"\r\nUser Access Verification\r\nUsername: "), time.sleep(1)))
    assert scanner.probe_host("127.0.0.1", port, 0.6) is None
    silent = serve(lambda s: time.sleep(2))
    assert scanner.probe_host("127.0.0.1", silent, 0.5) is None
    assert scanner.probe_host("127.0.0.1", closed_port(), 0.5) is None


def test_extron_backup_default_queries_are_read_only_identity(serve):
    sent = []
    port = serve(fake_extron(sent=sent))
    cfg = backup.collect_device_config("127.0.0.1", port, 1.0)
    assert "error" not in cfg and cfg["model"] == "DTP CrossPoint 84"
    assert cfg["raw"]["unit_name"] == "Courtroom-3A-MTX" and cfg["raw"]["ip_address"] == "192.168.1.20"
    # nothing with a leading digit other than the 1I/2I info queries: no "1Z", "1B", "0G"...
    assert set(sent) == {"I", "N", "Q", "*Q", "1I", "2I"} | {ESC + c + "\r" for c in
                                                             ("CN", "CH", "CI", "CS", "CG", "DH")}


def test_extron_backup_extra_queries_errors_and_silence(serve, tmp_path):
    q = tmp_path / "q.txt"
    q.write_text("# extras\nvolume = V\naudio_mute = Z\ninput = !\nname2 = \\eCN\\r\n")
    extra = backup.load_queries(str(q))
    assert extra["name2"] == ESC + "CN\r"
    port = serve(fake_extron())
    cfg = backup.collect_device_config("127.0.0.1", port, 0.6, extra_queries=extra)
    assert cfg["raw"]["volume"] == "Vol-20"
    assert "E10 (invalid command)" in cfg["unsupported"]["audio_mute"]
    assert "no response" in cfg["unsupported"]["input"]
    for bad in ("reset = \\eZXXX\\r", "boot = \\e1BOOT\\r", "just a line"):
        q.write_text(bad + "\n")
        with pytest.raises(ValueError):
            backup.load_queries(str(q))


def test_extron_backup_failures_are_reported_not_saved(serve):
    assert "cannot connect" in backup.collect_device_config("127.0.0.1", closed_port(), 0.5)["error"]
    port = serve(fake_extron(password="pw"))
    assert backup.collect_device_config("127.0.0.1", port, 0.6)["error"] == "password required"
    assert backup.collect_device_config("127.0.0.1", port, 0.6, password="no")["error"] == "password rejected"
    other = serve(lambda s: (s.sendall(b"login: "), time.sleep(1)))
    assert "does not look like" in backup.collect_device_config("127.0.0.1", other, 0.5)["error"]


def test_extron_backup_save_find_diff(serve, tmp_path):
    port = serve(fake_extron())
    first = backup.collect_device_config("127.0.0.1", port, 0.6)
    assert backup.find_latest_backup("127.0.0.1", tmp_path) is None
    p1 = backup.save_backup(first, "127.0.0.1", tmp_path)
    p2 = backup.save_backup(first, "127.0.0.1", tmp_path)       # same second -> distinct file
    assert p1 != p2 and backup.find_latest_backup("127.0.0.1", tmp_path) == p2
    assert backup.diff_configs(json.loads(p1.read_text()), first) == []
    changed = serve(fake_extron(state={ESC + "CI\r": "192.168.1.99"}))
    second = backup.collect_device_config("127.0.0.1", changed, 0.6)
    assert backup.diff_configs(first, second) == [("ip_address", "192.168.1.20", "192.168.1.99")]


def test_sis_helpers():
    assert describe_error("E24") == "E24 (privilege violation)" and describe_error("Vol20") is None
    assert decode_command(r"\eG40000AU\r") == ESC + "G40000AU\r"


def test_sis_busy_is_retried(serve):
    calls = []

    def handler(sock):
        while sock.recv(16):
            calls.append(1)
            sock.sendall(b"E22\r\n" if len(calls) < 3 else b"Vol-20\r\n")
    with SISDevice("127.0.0.1", serve(handler), 0.5) as dev:
        assert dev.query("V") == "Vol-20" and len(calls) == 3


# ----------------------------------------------------------------- Biamp

def fake_tesira(blocks=None, faults="", version="3.14.0.2", sent=None):
    blocks = blocks if blocks is not None else {
        "Level1": {"numChannels": 2, "level 1": -10.0, "level 2": -10.0, "mute 1": False, "mute 2": True},
        "Mixer1": {"numInputs": 2, "numOutputs": 1, "inputLevel 1": 0.0, "inputLevel 2": -6.0,
                   "inputMute 1": False, "inputMute 2": False, "outputLevel 1": 0.0, "outputMute 1": False,
                   "crosspointLevelState 1 1": True, "crosspointLevelState 2 1": False},
    }
    fault_list = ('[{"id":INDICATOR_NONE_IN_DEVICE "name":"No fault in device" "faults":[] "serialNumber":"0412"}]'
                  if not faults else
                  '[{"id":INDICATOR_MAJOR_IN_DEVICE "name":"Major Fault in Device" "faults":[{"id":FAULT_DANTE '
                  f'"name":"{faults}"}}] "serialNumber":"0412"}}]')

    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return f"{v:.6f}" if isinstance(v, float) else str(v)

    def handler(sock):
        sock.sendall(IAC + b"\xfd\x18" + IAC + b"\xfd\x20" + IAC + b"\xfb\x03" + IAC + b"\xfb\x01")
        got = b""
        while len(got) < 12:                      # insist on the refusals, like the real unit
            got += sock.recv(64)
        assert got == IAC + b"\xfc\x18" + IAC + b"\xfc\x20" + IAC + b"\xfe\x03" + IAC + b"\xfe\x01"
        sock.sendall(b"Welcome to the Tesira Text Protocol Server...\r\n")
        buf = b""
        while True:
            data = sock.recv(1024)
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                if sent is not None:
                    sent.append(cmd)
                tag, verb, *rest = cmd.split()
                attr = " ".join(rest)
                if cmd == "DEVICE get hostname":
                    reply = '+OK "value":"TesiraForte-3A"'
                elif cmd == "DEVICE get serialNumber":
                    reply = '+OK "value":"04121234"'
                elif cmd == "DEVICE get version":
                    reply = f'+OK "value":"{version}"'
                elif cmd == "DEVICE get activeFaultList":
                    reply = f'+OK "value":{fault_list}'
                elif cmd == "SESSION get aliases":
                    reply = '+OK "list":[' + " ".join(f'"{t}"' for t in ["DEVICE", *blocks]) + "]"
                elif verb == "get" and tag in blocks and attr in blocks[tag]:
                    reply = f'+OK "value":{fmt(blocks[tag][attr])}'
                else:
                    reply = '-ERR address not found: {"deviceId":0 "classCode":0 "instanceNum":0}'
                sock.sendall(line + b"\r\n" + reply.encode() + b"\r\n")      # telnet echo, then reply
    return handler


def test_ttp_value_parsing():
    assert parse_value('"value":-10.000000') == -10.0
    assert parse_value('"value":"3.14.0.2"') == "3.14.0.2"
    assert parse_value('"value":true') is True and parse_value('"value":3') == 3
    assert parse_value('"list":["Level1" "My Mixer"]') == ["Level1", "My Mixer"]
    assert fault_names('"value":[{"name":"No fault in device" "faults":[]}]') == []


def test_tesira_status_healthy_and_faulted(serve):
    ok = tstatus.poll_device("127.0.0.1", serve(fake_tesira()), 1.0, with_blocks=True)
    assert ok.healthy and (ok.hostname, ok.serial, ok.version) == ("TesiraForte-3A", "04121234", "3.14.0.2")
    assert ok.blocks == ["Level1", "Mixer1"]
    bad = tstatus.poll_device("127.0.0.1", serve(fake_tesira(faults="Dante module not responding")), 1.0)
    assert not bad.healthy and "Dante module not responding" in bad.faults
    down = tstatus.poll_device("127.0.0.1", closed_port(), 0.5)
    assert not down.reachable and not down.healthy and "cannot connect" in down.errors[0]


def test_tesira_only_ever_sends_get(serve):
    sent = []
    tdiff.get_device_snapshot("127.0.0.1", serve(fake_tesira(sent=sent)), 1.0)
    assert sent and all(re.fullmatch(r"\S+ get \S+( \d+){0,2}", c) for c in sent)


def test_tesira_snapshot_and_diff(serve, tmp_path):
    ref = tdiff.get_device_snapshot("127.0.0.1", serve(fake_tesira()), 1.0)
    assert ref["blocks"]["Level1"] == {"numChannels": 2, "level[1]": -10.0, "level[2]": -10.0,
                                       "mute[1]": False, "mute[2]": True}
    assert ref["blocks"]["Mixer1"]["crosspointLevelState[2,1]"] is False
    assert ref["blocks"]["Mixer1"]["inputLevel[2]"] == -6.0
    assert tdiff.diff_snapshots(ref, tdiff.get_device_snapshot("127.0.0.1", serve(fake_tesira()), 1.0)) == []

    other = {"Level1": {"numChannels": 2, "level 1": -10.02, "level 2": -4.0, "mute 1": False, "mute 2": False},
             "Extra1": {"numChannels": 1, "level 1": 0.0}}
    tgt = tdiff.get_device_snapshot("127.0.0.1", serve(fake_tesira(other, version="3.15.0.1")), 1.0)
    diffs = {(d["severity"], d["param"]): (d["reference"], d["target"]) for d in tdiff.diff_snapshots(ref, tgt)}
    assert diffs == {("warning", "firmware version"): ("3.14.0.2", "3.15.0.1"),
                     ("extra", "Extra1"): ("no such instance tag", "present"),
                     ("missing", "Mixer1"): ("present", "no such instance tag"),
                     ("mismatch", "Level1 level[2]"): (-10.0, -4.0),
                     ("mismatch", "Level1 mute[2]"): (True, False)}

    saved = tmp_path / "room1.json"
    saved.write_text(json.dumps(ref))
    assert tdiff.load_snapshot(str(saved), 23, 1.0, {"Level1"}, 32)["blocks"].keys() == {"Level1"}


def test_tesira_not_a_tesira(serve):
    port = serve(lambda s: (s.sendall(b"login: "), time.sleep(1.5)))
    with pytest.raises(TesiraError, match="is this a Tesira"):
        with Tesira("127.0.0.1", port, 0.4) as dsp:
            dsp.get("DEVICE", "hostname")


# -------------------------------------------------------------- Crestron

IPTABLE_3SERIES = """CIP_ID  Type    Status    DevID   Port   IP Address/SiteName
   3    Gway    ONLINE    1       41794  127.0.0.1
  1A    Gway    OFFLINE           41794  tsw-lobby.av.local
   5    Gway    ONLINE    2       41794  10.9.9.9
   5    Gway    ONLINE    3       41794  127.0.0.1
"""


def fake_crestron(login=None):
    def handler(sock):
        def read_line():
            got = b""
            while not got.endswith(b"\r"):
                chunk = sock.recv(256)
                if not chunk:
                    raise OSError
                got += chunk
            return got[:-1].decode()
        read_line()
        if login:
            sock.sendall(b"\r\nLogin: ")
            user = read_line()
            sock.sendall(b"\r\nPassword: ")
            if (user, read_line()) != login:
                sock.sendall(b"\r\nLogin incorrect\r\nLogin: ")
                return
        sock.sendall(b"\r\nCP3>")
        while True:
            cmd = read_line()
            body = {"VER": "CP3 Cntrl Eng [v1.8001.0146 (Dec  2 2022), #8A3B12C4] @E-00107f8a1b2c",
                    "IPTABLE": IPTABLE_3SERIES}.get(cmd.upper(), "Bad or Incomplete Command")
            sock.sendall(cmd.encode() + b"\r\n" + body.replace("\n", "\r\n").encode() + b"\r\nCP3>")
    return handler


def test_parse_iptable_both_column_orders():
    rows = iptable.parse_iptable(IPTABLE_3SERIES)
    assert [(r["ip_id"], r["status"], r["address"], r["port"], r["device_id"]) for r in rows] == [
        ("03", "ONLINE", "127.0.0.1", "41794", "1"), ("1A", "OFFLINE", "tsw-lobby.av.local", "41794", ""),
        ("05", "ONLINE", "10.9.9.9", "41794", "2"), ("05", "ONLINE", "127.0.0.1", "41794", "3")]
    legacy = iptable.parse_iptable("junk\n 03   CIP    ONLINE    192.168.1.201    41794   00000000\nCP3>")
    assert [(r["ip_id"], r["address"], r["port"]) for r in legacy] == [("03", "192.168.1.201", "41794")]
    wide = iptable.parse_iptable("CIP_ID Type Status DevID Port IP Address/SiteName Model Name\n"
                                 "  2B   Gway  ONLINE  4  41794  192.168.1.77   TSW-1070  Lobby panel")
    assert (wide[0]["address"], wide[0]["model"]) == ("192.168.1.77", "TSW-1070")


def test_crestron_check_processor(serve):
    import ipaddress
    r = iptable.check_processor("127.0.0.1", serve(fake_crestron()), 1.0,
                                expect_subnets=[ipaddress.IPv4Network("127.0.0.0/8")],
                                ping=lambda host: host == "127.0.0.1")
    assert r["reachable"] and r["prompt"] == "CP3" and r["firmware"].startswith("CP3 Cntrl Eng")
    assert len(r["ip_table"]) == 4
    issues = "\n".join(r["issues"])
    assert "IP ID 1A (Gway) → tsw-lobby.av.local — status OFFLINE" in issues
    assert "IP ID 1A (Gway) → tsw-lobby.av.local — no ping reply" in issues
    assert "IP ID 05 (Gway) → 10.9.9.9 — outside expected subnet (127.0.0.0/8)" in issues
    assert "IP ID 05 appears 2 times" in issues
    assert "IP ID 03" not in issues


def test_crestron_login_and_failures(serve):
    port = serve(fake_crestron(login=("admin", "pw")))
    assert "requires a login" in iptable.check_processor("127.0.0.1", port, 0.6, ping=None)["issues"][0]
    assert "login rejected" in iptable.check_processor("127.0.0.1", port, 0.6, "admin", "no", ping=None)["issues"][0]
    ok = iptable.check_processor("127.0.0.1", port, 0.6, "admin", "pw", ping=None)
    assert ok["reachable"] and len(ok["ip_table"]) == 4
    dead = iptable.check_processor("127.0.0.1", closed_port(), 0.4, ping=None)
    assert not dead["reachable"]
    mute = iptable.check_processor("127.0.0.1", serve(lambda s: time.sleep(1.5)), 0.4, ping=None)
    assert "no console prompt" in mute["issues"][0]


# ----------------------------------------------------------------- Q-SYS

def fake_core(components, logon=None, design_code="abc123"):
    def handler(sock):
        sock.sendall(json.dumps({"jsonrpc": "2.0", "method": "EngineStatus",
                                 "params": {"State": "Active"}}).encode() + b"\x00")
        authed = logon is None
        buf = b""
        while True:
            data = sock.recv(4096)
            if not data:
                return
            buf += data
            while b"\x00" in buf:
                raw, buf = buf.split(b"\x00", 1)
                req = json.loads(raw)
                m, p = req["method"], req.get("params")
                reply = {"jsonrpc": "2.0", "id": req["id"]}
                if m == "Logon":
                    authed = (p["User"], p["Password"]) == logon
                    reply.update({"result": True} if authed else
                                 {"error": {"code": 10, "message": "Logon required"}})
                elif not authed:
                    reply["error"] = {"code": 10, "message": "Logon required"}
                elif m == "StatusGet":
                    reply["result"] = {"Platform": "Core 110f", "State": "Active", "DesignName": "Courtroom",
                                       "DesignCode": design_code, "Status": {"Code": 0, "String": "OK"}}
                elif m == "Component.GetComponents":
                    reply["result"] = [{"Name": n, "Type": "gain", "Properties": []} for n in components]
                elif m == "Component.GetControls":
                    reply["result"] = {"Name": p["Name"], "Controls": components[p["Name"]]}
                else:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
                sock.sendall(json.dumps(reply).encode() + b"\x00")
    return handler


def _gain(db, mute, meter):
    return [{"Name": "gain", "Type": "Float", "Value": db, "String": f"{db}dB", "Direction": "Read/Write"},
            {"Name": "mute", "Type": "Boolean", "Value": mute, "String": "muted" if mute else "unmuted",
             "Direction": "Read/Write"},
            {"Name": "meter.1", "Type": "Float", "Value": meter, "String": f"{meter}dB", "Direction": "Read"}]


def test_qrc_snapshot_and_diff(serve, tmp_path):
    a = qrc.take_snapshot("127.0.0.1", serve(fake_core({"Mic Gain": _gain(-6.0, False, -30.1),
                                                        "PA Gain": _gain(0.0, False, -12.0)})), 1.0)
    assert (a["design"], a["platform"], a["status"]) == ("Courtroom", "Core 110f", "OK")
    assert a["components"]["Mic Gain"]["controls"]["gain"]["value"] == -6.0
    b = qrc.take_snapshot("127.0.0.1", serve(fake_core({"Mic Gain": _gain(-6.02, True, -55.0),
                                                        "Aux Gain": _gain(0.0, False, -90.0)},
                                                       design_code="zzz")), 1.0)
    diffs = {(d["kind"], d["where"]): (d["reference"], d["target"]) for d in qrc.diff_snapshots(a, b)}
    assert diffs == {("design", "design code (different compiled designs)"):
                     ("Courtroom [abc123]", "Courtroom [zzz]"),
                     ("extra", "Aux Gain"): ("no such component", "present"),
                     ("missing", "PA Gain"): ("present", "no such component"),
                     ("value", "Mic Gain : mute"): ("unmuted", "muted")}
    assert any(d["where"] == "Mic Gain : meter.1" for d in qrc.diff_snapshots(a, b, include_read=True))
    assert qrc.diff_snapshots(a, a) == []
    f = tmp_path / "a.json"
    f.write_text(json.dumps(a))
    assert qrc.load_snapshot(str(f), 1710, 1.0, None, None)["components"] == a["components"]


def test_qrc_logon_and_errors(serve):
    port = serve(fake_core({}, logon=("tech", "1234")))
    with pytest.raises(qrc.QRCError, match="Logon required"):
        qrc.take_snapshot("127.0.0.1", port, 1.0)
    assert qrc.take_snapshot("127.0.0.1", port, 1.0, "tech", "1234")["components"] == {}
    with pytest.raises(qrc.QRCError, match="timed out"):
        qrc.take_snapshot("127.0.0.1", serve(lambda s: time.sleep(1.5)), 0.4)
    with pytest.raises(OSError):
        qrc.take_snapshot("127.0.0.1", closed_port(), 0.4)


# --------------------------------------------------------------- monitor

def test_monitor_alerts_only_on_confirmed_changes():
    d = monitor.Device("Core", "10.0.0.1", 1710, fail_after=2)
    assert d.update(True) is None                       # first check up: no "back online" spam
    assert d.update(False) is None                      # one miss is not an outage
    assert "DOWN" in d.update(False)
    assert d.update(False) is None                      # still down: no repeat
    assert "UP" in d.update(True)
    assert d.update(True) is None
    cold = monitor.Device("Amp", "10.0.0.2", 80, fail_after=3)
    assert "DOWN" in cold.update(False)                 # down at startup is reported at once


def test_monitor_load_and_check(serve, tmp_path):
    up = serve(lambda s: None)
    j = tmp_path / "d.json"
    j.write_text(json.dumps([{"name": "Up", "ip": "127.0.0.1", "port": up},
                             {"ip": "127.0.0.1", "port": closed_port()}]))
    devices = monitor.load_devices(str(j), timeout=0.5)
    log = tmp_path / "log.csv"
    assert monitor.run_check(devices, log_path=str(log), quiet=True) == 1
    assert [d.online for d in devices] == [True, False] and devices[1].name == "127.0.0.1"
    assert log.read_text().splitlines()[0] == "timestamp,name,ip,port,state" and ",down" in log.read_text()

    c = tmp_path / "ips.csv"
    c.write_text("IP Schedule\nDevice Name,IP Address,Port\nCore,192.168.1.10,1710\nPanel,192.168.1.11,\n")
    loaded = monitor.load_devices(str(c))
    assert [(d.name, d.ip, d.port) for d in loaded] == [("Core", "192.168.1.10", 1710), ("Panel", "192.168.1.11", 80)]
    for bad in ('{"x": 1}', '[{"name": "no ip"}]', '[{"ip": "1.2.3.4", "port": 99999}]'):
        j.write_text(bad)
        with pytest.raises(ValueError):
            monitor.load_devices(str(j))


# ------------------------------------------- report recognises real output

def test_report_classifies_what_each_tool_actually_emits(serve):
    from dataclasses import asdict
    report = load_script("general/commissioning-report.py")

    t = tstatus.poll_device("127.0.0.1", serve(fake_tesira()), 1.0)
    outputs = {
        "tesira_status": [{**asdict(t), "healthy": t.healthy}],
        "crestron_iptable": [iptable.check_processor("127.0.0.1", serve(fake_crestron()), 1.0, ping=None)],
        "extron_scan": [asdict(scanner.probe_host("127.0.0.1", serve(fake_extron()), 1.0))],
        "extron_backup": [backup.collect_device_config("127.0.0.1", serve(fake_extron()), 0.6)],
        "monitor_status": [monitor.Device("Core", "10.0.0.1", 1710).as_dict()],
        "qsys_snapshot": qrc.take_snapshot("127.0.0.1", serve(fake_core({"PA": _gain(0.0, True, -20.0)})), 1.0),
        "tesira_snapshot": tdiff.get_device_snapshot("127.0.0.1", serve(fake_tesira()), 1.0),
    }
    for expected, data in outputs.items():
        data = json.loads(json.dumps(data))                 # exactly what lands in the file
        builder = report.classify(data)
        assert builder is not None and builder.__name__ == expected, expected
        title, status, summary, lines = builder(data)
        assert title and summary and lines
    assert report.qsys_snapshot(outputs["qsys_snapshot"])[2].endswith("1 muted")
    assert report.tesira_snapshot(outputs["tesira_snapshot"])[2] == "2 blocks, 1 muted"
