"""The Vectorworks export script, run against a stand-in for the `vs` module."""
import csv
import io
import sys
import types

from conftest import load_script


class FakeVS(types.ModuleType):
    """Just enough of Vectorworks' `vs` API, backed by a list of dict objects."""

    def __init__(self, objects, formats, doc_path):
        super().__init__("vs")
        self.objects, self.formats, self.doc_path = objects, formats, doc_path
        self.alerts, self.answer = [], "?"

    def ForEachObject(self, callback, criteria):
        for o in self.objects:
            if (criteria == "ALL" or criteria == "(R IN ['%s'])" % o.get("record", "\0")
                    or criteria == "(PON='%s')" % o.get("pio", "\0")):
                callback(o)

    def GetParametricRecord(self, h):
        return ("fmt", h["pio"]) if h.get("pio") else None

    def NumRecords(self, h):
        return 1 if h.get("record") or h.get("pio") else 0

    def GetRecord(self, h, i):
        return ("fmt", h.get("record") or h["pio"])

    def GetObject(self, name):
        return ("fmt", name) if name in self.formats else None

    def GetName(self, h):
        return h[1] if isinstance(h, tuple) else h.get("name", "")

    def NumFields(self, rec):
        return len(self.formats[rec[1]])

    def GetFldName(self, rec, i):
        return self.formats[rec[1]][i - 1]

    def GetRField(self, h, rec, field):
        return h["fields"].get(field, "")

    def GetLayer(self, h):
        return h.get("layer")

    def GetLName(self, layer):
        return layer

    def GetClass(self, h):
        return h.get("class", "None")

    def GetTypeN(self, h):
        return 15 if h.get("symbol") else 86

    def GetSymName(self, h):
        return h.get("symbol", "")

    def GetSymLoc(self, h):
        return h.get("loc", (0.0, 0.0))

    def GetFPathName(self):
        return self.doc_path

    def StrDialog(self, prompt, default):
        return self.answer

    def DidCancel(self):
        return False

    def AlrtDialog(self, text):
        self.alerts.append(text)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(io.StringIO(f.read())))


def test_export_records_and_plugin_objects(tmp_path, monkeypatch):
    objects = [
        {"name": "DSP-1", "record": "AV Equipment", "symbol": "Core110f", "layer": "Rack", "class": "AV",
         "loc": (12.5, 3.0), "fields": {"Make": "QSC", "Model": "Core 110f"}},
        {"name": "AMP-1", "record": "AV Equipment", "layer": "Rack", "fields": {"Make": "QSC", "Model": "SPA4-100"}},
        {"name": "TX-1", "pio": "Device", "layer": "Plan", "fields": {"Name": "TX-1", "Make": "Extron"}},
        {"name": "wall"},
    ]
    fake = FakeVS(objects, {"AV Equipment": ["Make", "Model"], "Device": ["Name", "Make"]},
                  str(tmp_path / "Courthouse.vwx"))
    monkeypatch.setitem(sys.modules, "vs", fake)
    mod = load_script("vectorworks/vw-export-records.py")     # importing runs main() with "?"
    assert "AV Equipment  (2)" in fake.alerts[0] and "Device  (1)" in fake.alerts[0]

    path, count = mod.export("AV Equipment")
    rows = read_csv(path)
    assert count == 2 and path == str(tmp_path / "Courthouse - AV Equipment.csv")
    assert rows[0] == ["Object Name", "Class", "Layer", "Symbol", "X", "Y", "Make", "Model"]
    assert rows[1] == ["DSP-1", "AV", "Rack", "Core110f", "12.500", "3.000", "QSC", "Core 110f"]

    path, count = mod.export("Device")
    assert count == 1 and read_csv(path)[1][-2:] == ["TX-1", "Extron"]
    assert mod.export("Nope") == (None, 0)

    # ...and the CSV feeds straight into the reconcile tool's loader.
    from avlib.bom import load_bom
    items, _ = load_bom(tmp_path / "Courthouse - AV Equipment.csv")
    assert [(i.manufacturer, i.model, i.location) for i in items] == [
        ("QSC", "Core 110f", "Rack"), ("QSC", "SPA4-100", "Rack")]
