"""Spreadsheet-driven tools: tabular loader, D-Tools BOM tools, cable schedule, reconcile."""
import json
import subprocess
import sys
import zipfile

import pytest
from conftest import EXAMPLES, ROOT, load_script

from avlib import tabular
from avlib.bom import load_bom

audit = load_script("dtools/bom-audit.py")
bomdiff = load_script("dtools/bom-diff.py")
racks = load_script("dtools/rack-loads.py")
reconcile = load_script("general/bom-reconcile.py")
cables = load_script("general/cable-schedule.py")

REV_A = EXAMPLES / "dtools-bom-rev-a.csv"
REV_B = EXAMPLES / "dtools-bom-rev-b.csv"


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / script), *map(str, args)],
                          capture_output=True, text=True)


# ---------------------------------------------------------------- tabular

@pytest.mark.parametrize("text,feet", [
    ("25", 25), ("25 ft", 25), ("25'", 25), ("25'-6\"", 25.5), ("25' 6", 25.5),
    ("10m", 32.8084), ("1,200", 1200), ("3048 mm", 10.0), ("120 in", 10), ("", None), ("n/a", None),
])
def test_to_feet(text, feet):
    got = tabular.to_feet(text)
    assert got is None if feet is None else got == pytest.approx(feet, rel=1e-4)


def test_to_feet_default_unit_metres():
    assert tabular.to_feet("10", "m") == pytest.approx(32.8084)


@pytest.mark.parametrize("text,number", [
    ("$1,234.50", 1234.5), ("(120.00)", -120.0), ("2 RU", 2.0), ("", 0.0), ("-3", -3.0), ("abc", 0.0),
])
def test_to_number(text, number):
    assert tabular.to_number(text) == number


def test_norm_key_ignores_spacing_and_case():
    assert tabular.norm_key("DMP 128 Plus C AT") == tabular.norm_key("dmp128plus-c at")


def test_header_found_below_title_rows_and_exact_alias_wins(tmp_path):
    f = tmp_path / "bom.csv"
    f.write_text("My Project\n\nModel Description,Mfr,Model,Qty\nA thing,Acme,X-1,3\n")
    items, table = load_bom(f)
    assert table.header_row == 3
    assert (items[0].manufacturer, items[0].model, items[0].quantity) == ("Acme", "X-1", 3)


def test_missing_required_column_names_the_fix(tmp_path):
    f = tmp_path / "bom.csv"
    f.write_text("Brand,Thing,Qty\nAcme,X-1,3\n")
    with pytest.raises(tabular.TableError) as e:
        load_bom(f)
    assert "--map" in str(e.value) and "Thing" in str(e.value)
    items, _ = load_bom(f, overrides={"model": "Thing"})
    assert items[0].model == "X-1"


def test_encodings_and_delimiters(tmp_path):
    utf16 = tmp_path / "vw.txt"
    utf16.write_text("Make\tModel\nShure\tMXA920\n", encoding="utf-16")
    assert load_bom(utf16)[0][0].model == "MXA920"
    semi = tmp_path / "eu.csv"
    semi.write_bytes("Manufacturer;Model;Qty\nGen\xe9lec;8010;2\n".encode("cp1252"))
    assert load_bom(semi)[0][0].manufacturer == "Genélec"


def _write_xlsx(path, rows):
    """Minimal .xlsx with shared strings, numeric cells and a skipped (empty) cell."""
    strings, sheet_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if value is None:
                continue
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                if value not in strings:
                    strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{strings.index(value)}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships"><sheets><sheet name="BOM" sheetId="1" r:id="rId1"/>'
                   '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="x" '
                   'Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings)
                   + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}'
                   "</sheetData></worksheet>")


def test_xlsx_without_third_party_libs(tmp_path):
    f = tmp_path / "bom.xlsx"
    _write_xlsx(f, [["Manufacturer", "Model", "Qty", "Unit Price"],
                    ["QSC", "Core 110f", 2, 3500.0], ["Extron", "IN1608", None, 1200.5]])
    items, _ = load_bom(f)
    assert len(items) == 1          # the row with no quantity is a spacer
    assert (items[0].model, items[0].quantity, items[0].unit_price) == ("Core 110f", 2, 3500)
    with pytest.raises(tabular.TableError, match="no sheet named"):
        load_bom(f, sheet="Nope")


def test_openpyxl_written_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Manufacturer", "Model", "Qty", "Unit Price"])
    ws.append(["QSC", "Core 110f", 2, 3500.25])
    f = tmp_path / "real.xlsx"
    wb.save(f)
    items, _ = load_bom(f)
    assert (items[0].manufacturer, items[0].quantity, items[0].unit_price) == ("QSC", 2, 3500.25)


# ---------------------------------------------------------------- D-Tools

def test_audit_finds_the_planted_problems():
    items, table = load_bom(REV_A)
    found = {(f["level"], f["message"].split(" (")[0].split(" '")[0]) for f in audit.audit(items, table)}
    assert ("ERROR", "priced below cost") in found
    assert ("WARNING", "placeholder model") in found
    assert any("different manufacturers: Q-SYS, QSC" in f["message"] for f in audit.audit(items, table))
    assert any("margin 12.2%" in f["message"] for f in audit.audit(items, table))
    assert not any("margin" in f["message"] for f in audit.audit(items, table, min_margin=10)
                   if f["level"] == "WARNING" and "floor" in f["message"])


def test_audit_totals_and_exit_codes():
    items, _ = load_bom(REV_A)
    t = audit.totals(items)["TOTAL"]
    assert (t["price"], t["cost"], t["margin_pct"]) == (51890.0, 36070.0, 30.5)
    assert run("dtools/bom-audit.py", REV_A).returncode == 1
    out = run("dtools/bom-audit.py", REV_A, "--json")
    assert json.loads(out.stdout)["counts"]["ERROR"] == 1
    assert run("dtools/bom-audit.py", "nope.csv").returncode == 2


def test_audit_same_part_two_prices(tmp_path):
    f = tmp_path / "b.csv"
    f.write_text("Manufacturer,Model,Qty,Unit Price,Unit Cost\nA,X1,1,100,50\nA,x-1,2,120,50\n")
    items, table = load_bom(f)
    assert any("2 different sell prices" in x["message"] for x in audit.audit(items, table))


def test_bom_diff():
    old, new = load_bom(REV_A)[0], load_bom(REV_B)[0]
    changes = {(c["change"], c["model"]): c for c in bomdiff.diff_boms(old, new)}
    assert set(changes) == {("added", "FW-85BZ40L"), ("removed", "QM85C"),
                            ("price", "TSW-1070-B-S"), ("quantity", "AD-C6T")}
    assert changes[("quantity", "AD-C6T")]["price_delta"] == 860.0
    assert sum(c["price_delta"] for c in changes.values()) == 2860.0
    assert bomdiff.diff_boms(old, old) == []
    assert run("dtools/bom-diff.py", REV_A, REV_A).returncode == 0
    md = run("dtools/bom-diff.py", REV_A, REV_B, "--format", "markdown")
    assert md.returncode == 1 and "**Net sell change: $2,860.00**" in md.stdout


def test_bom_diff_by_location_sees_a_move(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("Manufacturer,Model,Qty,Location\nA,X1,2,Room 1\n")
    b.write_text("Manufacturer,Model,Qty,Location\nA,X1,2,Room 2\n")
    assert bomdiff.diff_boms(load_bom(a)[0], load_bom(b)[0]) == []
    moved = bomdiff.diff_boms(load_bom(a)[0], load_bom(b)[0], by_location=True)
    assert sorted(c["change"] for c in moved) == ["added", "removed"]


def test_rack_loads():
    items, _ = load_bom(REV_A)
    loads = {g["name"]: g for g in racks.compute_loads(items, only=["rack"], rack_ru=44)}
    r1 = loads["Rack 1 - Courtroom 3A"]
    assert r1["watts"] == 1845 and r1["ru"] == 7 and r1["circuits"] == 1
    assert r1["amps"] == pytest.approx(15.4, abs=0.05)
    assert r1["btu"] == pytest.approx(1845 * 3.412, abs=1)
    assert "Courtroom 3A" not in loads
    # 16 A is exactly one 20 A circuit at 80%; a watt more needs two
    one = racks.compute_loads([type(items[0])(row=1, model="x", location="R", watts=1920)])[0]
    two = racks.compute_loads([type(items[0])(row=1, model="x", location="R", watts=1921)])[0]
    assert (one["circuits"], two["circuits"]) == (1, 2)


def test_rack_loads_flags_missing_power_and_overflow():
    Item = type(load_bom(REV_A)[0][0])
    g = racks.compute_loads([Item(row=1, model="Amp", location="R", rack_units=2, quantity=3)],
                            rack_ru=4)[0]
    assert g["ru"] == 6 and any("over the 4 RU" in w for w in g["warnings"])
    assert any("no power data" in w for w in g["warnings"])


# -------------------------------------------------------------- reconcile

def test_reconcile_example():
    bom, drawn = load_bom(REV_A)[0], load_bom(EXAMPLES / "vectorworks-devices.csv")[0]
    r = reconcile.reconcile(bom, drawn, ignore=["*cable*"])
    assert [x["item"] for x in r["not_in_bom"]] == ["PTZOptics PT30X-4K-G3"]
    assert {x["item"] for x in r["not_on_drawing"]} == {"QSC AD-C6T", "Middle Atlantic BGR-4532"}
    assert {(x["item"], x["bom"], x["drawing"]) for x in r["quantity"]} == {
        ("QSC Core 110f", 2, 1), ("Shure MXA920W-S", 2, 3)}
    assert r["matched"] == 7          # incl. "231 D" on the BOM vs "231D" on the drawing


def test_reconcile_suggests_near_matches_and_by_location(tmp_path):
    bom, dwg = tmp_path / "bom.csv", tmp_path / "dwg.csv"
    bom.write_text("Manufacturer,Model,Qty,Location\nExtron,DMP 128 Plus C,1,Rm 1\nQSC,K8.2,2,Rm 1\n"
                   "QSC,K8.2,2,Rm 2\n")
    dwg.write_text("Make,Model,Room\nExtron,DMP 128 Plus C AT\nQSC,K8.2,Rm 1\nQSC,K8.2,Rm 1\n"
                   "QSC,K8.2,Rm 1\nQSC,K8.2,Rm 2\n")
    b, d = load_bom(bom)[0], load_bom(dwg)[0]
    overall = reconcile.reconcile(b, d)
    assert overall["quantity"] == [] and overall["not_in_bom"][0]["possible_match"] == "Extron DMP 128 Plus C"
    per_room = reconcile.reconcile(b, d, by_location=True)
    assert {(q["location"], q["bom"], q["drawing"]) for q in per_room["quantity"]} == {
        ("Rm 1", 2, 3), ("Rm 2", 2, 1)}


# ---------------------------------------------------------- cable schedule

def test_cable_json_roundtrip_and_units(tmp_path):
    data = cables.load_schedule(EXAMPLES / "cable-schedule.json")
    c = data["rooms"][0]["cables"][0]
    assert (c["length_m"], c["length_ft"]) == (15, 49.2)
    f = tmp_path / "ft.json"
    f.write_text(json.dumps({"rooms": [{"name": "R", "cables": [{"id": "1", "length_ft": 100}]}]}))
    assert cables.load_schedule(f)["rooms"][0]["cables"][0]["length_m"] == 30.5
    again = tmp_path / "again.json"
    again.write_text(cables.generate_table(data, "json"))
    assert cables.load_schedule(again) == data


def test_cable_spreadsheet_import_reads_metres_from_header():
    data = cables.load_schedule(EXAMPLES / "connectcad-circuits.csv")
    c = data["rooms"][0]["cables"][1]
    assert (c["id"], c["type"], c["signal"], c["from"], c["from_port"], c["to_port"], c["length_m"]) == (
        "V-002", "CAT6A STP", "HDBaseT", "TX-1", "DTP OUT", "DTP IN 5", 42)
    assert data["rooms"][0]["name"] == "Courtroom 3A"


def test_cable_check_example():
    found = {(lvl, cid, msg.split("  [")[0]) for lvl, cid, msg in
             cables.check_schedule(cables.load_schedule(EXAMPLES / "connectcad-circuits.csv"))}
    assert ("ERROR", "V-003", "104 m exceeds the 100 m channel limit for category cable") in found
    assert ("ERROR", "C-002", "missing destination") in found
    assert any(lvl == "ERROR" and cid == "A-001" and "duplicate" in msg for lvl, cid, msg in found)
    assert any(cid == "V-004" and "passive HDMI" in msg for _, cid, msg in found)
    assert any(cid == "C-001" and "USB" in msg for _, cid, msg in found)
    assert not any(cid in ("V-001", "V-002") for _, cid, _ in found)
    assert cables.check_schedule(cables.load_schedule(EXAMPLES / "cable-schedule.json")) == []
    assert run("general/cable-schedule.py", EXAMPLES / "connectcad-circuits.csv", "--check").returncode == 1


def test_cable_takeoff_and_formats():
    data = cables.load_schedule(EXAMPLES / "connectcad-circuits.csv")
    rows = {r["type"]: r for r in cables.bulk_takeoff(data, slack_pct=15, spool_ft=500)}
    assert rows["CAT6A STP"]["count"] == 2 and rows["CAT6A STP"]["spools"] == 2
    assert rows["CAT6A STP"]["order_feet"] == pytest.approx(479.0 * 1.15, abs=0.2)
    tsv = cables.generate_table(data, "tsv").splitlines()
    assert tsv[0].split("\t")[:4] == ["ID", "Room", "Type", "Signal"] and len(tsv) == 10
    assert len(cables.generate_table(data, "labels").splitlines()) == 19
    data["rooms"][0]["cables"][0]["notes"] = "a|b\nc"
    assert "a\\|b c" in cables.generate_table(data, "markdown")


def test_cable_cli_errors_and_example(tmp_path):
    assert run("general/cable-schedule.py").returncode == 2
    assert run("general/cable-schedule.py", "missing.json").returncode == 2
    bad = tmp_path / "bad.json"
    bad.write_text("[1,2]")
    assert run("general/cable-schedule.py", bad).returncode == 2
    assert json.loads(run("general/cable-schedule.py", "--example").stdout)["rooms"]


# ------------------------------------------------- second batch of tools

to_import = load_script("dtools/drawing-to-import.py")
ipsched = load_script("general/ip-schedule.py")
report = load_script("general/commissioning-report.py")
DEVICES = EXAMPLES / "vectorworks-devices.csv"


def test_drawing_to_import_rolls_up_and_finds_the_delta():
    drawn, bom = load_bom(DEVICES)[0], load_bom(REV_A)[0]
    rows, skipped = to_import.build_import(drawn)
    by_model = {(r["Model"], r["Location"]): r["Quantity"] for r in rows}
    assert by_model[("SPA4-100", "Rack 1 - Courtroom 3A")] == 2
    assert by_model[("DTP T HWP 4K 231D", "Courtroom 3A")] == 4 and skipped == []
    assert sum(r["Quantity"] for r in rows) == 18

    delta, _ = to_import.build_import(drawn, existing_items=bom)
    assert [(r["Model"], r["Quantity"]) for r in delta] == [("PT30X-4K-G3", 1), ("MXA920W-S", 1)]
    project_wide, _ = to_import.build_import(drawn, by_location=False, existing_items=bom, phase="Trim")
    assert [(r["Model"], r["Location"], r["Phase"]) for r in project_wide] == [
        ("PT30X-4K-G3", "", "Trim"), ("MXA920W-S", "", "Trim")]


def test_drawing_to_import_cli_output_reloads_as_a_bom(tmp_path):
    out = tmp_path / "import.csv"
    done = run("dtools/drawing-to-import.py", DEVICES, "--out", out)
    assert done.returncode == 0 and "18 unit(s)" in done.stderr
    items, table = load_bom(out)
    assert sum(i.quantity for i in items) == 18 and table.has("location")
    tbd = tmp_path / "tbd.csv"
    tbd.write_text("Make,Model\nAcme,TBD\nAcme,X1\n")
    done = run("dtools/drawing-to-import.py", tbd)
    assert "skipped row 2" in done.stderr and "Acme,X1" in done.stdout and "TBD" not in done.stdout


def test_ip_schedule_build():
    items = load_bom(REV_A)[0]
    rows, left_out = ipsched.build_schedule(items, "192.168.10.0/24", start=10)
    assert len(rows) == 13 and len({r["IP Address"] for r in rows}) == 13
    assert rows[0]["IP Address"] == "192.168.10.10" and rows[0]["Gateway"] == "192.168.10.1"
    assert {it.model for it in left_out} == {"AD-C6T", "DTP T HWP 4K 231 D", "BGR-4532", "TBD"}
    ports = {r["Model"]: r["Port"] for r in rows}
    assert (ports["Core 110f"], ports["SPA4-100"], ports["CP4N"], ports["MXA920W-S"]) == (1710, 80, 41794, 2202)
    assert len({r["Name"] for r in rows}) == 13

    more, _ = ipsched.build_schedule(items, "192.168.10.0/24", include=["*wallplate*"], exclude=["*display*"])
    models = [r["Model"] for r in more]
    assert models.count("DTP T HWP 4K 231 D") == 4 and "QM85C" not in models
    # the gateway is never handed out, and a subnet that is too small says so
    tight, _ = ipsched.build_schedule(items[:1], "10.0.0.0/29", start=1, gateway="10.0.0.1")
    assert tight[0]["IP Address"] == "10.0.0.2"
    with pytest.raises(ValueError, match="no addresses left"):
        ipsched.build_schedule(items, "10.0.0.0/29", start=1)


def test_ip_schedule_verify():
    rows = [{"name": "Core", "ip": "192.168.1.10", "mask": "255.255.255.0", "gateway": "192.168.1.1", "port": "1710"},
            {"name": "Panel", "ip": "192.168.1.10", "mask": "255.255.255.0", "gateway": "192.168.1.1", "port": ""},
            {"name": "Amp", "ip": "192.168.1.255", "mask": "255.255.255.0", "gateway": "192.168.2.1"},
            {"name": "Amp", "ip": "192.168.1.30"}, {"name": "Cam", "ip": "not-an-ip"}, {"name": "Spare", "ip": ""}]
    found = {(lvl, name, msg) for lvl, name, msg in ipsched.verify_schedule(rows, offline=True)}
    assert ("ERROR", "Core", "192.168.1.10 is assigned 2 times: Core, Panel") in found
    assert ("ERROR", "Amp", "192.168.1.255 is the network or broadcast address of 192.168.1.0/24") in found
    assert ("ERROR", "Amp", "gateway 192.168.2.1 is not in 192.168.1.0/24") in found
    assert ("ERROR", "Cam", "'not-an-ip' is not a valid IPv4 address") in found
    assert ("WARNING", "Amp", "name used 2 times (192.168.1.255, 192.168.1.30)") in found
    assert ("WARNING", "Spare", "no IP address assigned") in found

    probed = []
    live = ipsched.verify_schedule(rows[:2], probe=lambda ip, port, t: probed.append((ip, port)) or port == 1710)
    assert sorted(probed) == [("192.168.1.10", 80), ("192.168.1.10", 1710)]
    assert ("ERROR", "Panel", "192.168.1.10 does not answer on port 80") in live


def test_ip_schedule_cli_roundtrip_feeds_the_monitor(tmp_path):
    out = tmp_path / "ips.csv"
    assert run("general/ip-schedule.py", "build", REV_A, "--subnet", "10.9.8.0/24", "--out", out).returncode == 0
    assert run("general/ip-schedule.py", "verify", out, "--offline").returncode == 0
    monitor = load_script("general/device-monitor.py")
    devices = monitor.load_devices(str(out))
    assert len(devices) == 13 and devices[0].ip == "10.9.8.10" and devices[0].port == 41794
    assert run("general/ip-schedule.py", "build", REV_A, "--subnet", "nonsense").returncode == 2


def test_commissioning_report(tmp_path):
    files = {
        "tesira.json": [{"host": "10.0.0.5", "hostname": "DSP-3A", "serial": "0412", "version": "3.14",
                         "healthy": False, "faults": ["Dante module not responding"], "errors": []}],
        "crestron.json": [{"host": "10.0.0.6", "reachable": True, "firmware": "CP4N v2.8", "issues": [],
                           "ip_table": [{"ip_id": "03", "type": "Gway", "status": "ONLINE",
                                         "address": "10.0.0.20", "model": "TSW-1070"}]}],
        "extron.json": [{"ip": "10.0.0.7", "port": 23, "model": "IN1608", "firmware": "1.07",
                         "part_number": "60-1238-01", "name": "SW|1", "locked": True}],
        "uptime.json": [{"name": "Core", "ip": "10.0.0.8", "port": 1710, "online": True, "failures": 0}],
        "core.json": {"host": "10.0.0.8", "design": "Courtroom", "platform": "Core 110f", "state": "Active",
                      "status": "OK", "status_code": 0, "captured": "2026-01-01",
                      "components": {"PA": {"type": "gain", "controls": {"mute": {"value": 1}, "gain": {"value": 0}}}}},
        "dsp-snap.json": {"host": "10.0.0.5", "hostname": "DSP-3A", "blocks": {"Level1": {"mute[1]": True}}},
        "mystery.json": {"hello": "world"},
        "broken.json": None,
    }
    for name, data in files.items():
        (tmp_path / name).write_text("{not json" if data is None else json.dumps(data))
    (tmp_path / "bom.json").write_text(run("dtools/bom-audit.py", REV_A, "--json").stdout)

    md, failed, built = report.build_report(sorted(tmp_path.glob("*.json")), "Courtroom 3A", "S. Paige")
    assert failed and built == 7
    assert "# Commissioning Report — Courtroom 3A" in md and "by S. Paige" in md
    assert "**Result: FAIL — 2 check(s) need attention**" in md       # Tesira fault + BOM below-cost line
    assert "| ❌ | Biamp Tesira status | 0/1 healthy | `tesira.json` |" in md
    assert "| ✅ | Crestron IP tables | 1 processor(s), 1 entries, 0 issue(s) | `crestron.json` |" in md
    assert "Dante module not responding" in md and "SW\\|1" in md and "password set" in md
    assert "- PA : mute" in md and "- Level1 mute[1]" in md
    assert "priced below cost" in md
    assert "`mystery.json` — not a format this report recognises" in md
    assert "`broken.json` — could not be read" in md

    done = run("general/commissioning-report.py", tmp_path, "--out", tmp_path / "r.md")
    assert done.returncode == 1 and (tmp_path / "r.md").read_text(encoding="utf-8") .startswith("# Commissioning")
    only_ok = run("general/commissioning-report.py", tmp_path / "uptime.json", tmp_path / "crestron.json")
    assert only_ok.returncode == 0 and "**Result: PASS**" in only_ok.stdout
    assert run("general/commissioning-report.py", tmp_path / "mystery.json").returncode == 2
