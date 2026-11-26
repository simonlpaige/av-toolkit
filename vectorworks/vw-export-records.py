"""
vw-export-records.py — Export record / plug-in object data from Vectorworks to CSV.

RUNS INSIDE VECTORWORKS, not from a terminal. It writes one CSV row per
object carrying a given record format (or per plug-in object of a given
type, such as ConnectCAD devices), with every field as a column plus
the object's name, class, layer, symbol and position.

That CSV is what the rest of the toolkit reads:

    python general/bom-reconcile.py dtools-bom.csv "MyProject - Equipment.csv"
    python general/cable-schedule.py "MyProject - Circuit.csv" --check

To install:
    Resource Manager > New Resource > Script. Name it, choose
    "Python Script" as the language, paste this whole file, OK.
    Double-click the script to run it.

When it runs it asks for a name:
    ?                 list every record format and plug-in object type in
                      the drawing with a count — start here if you don't
                      know what your device objects are called
    <record name>     export all objects with that record attached
    <plug-in name>    export all plug-in objects of that type

The CSV is saved beside the .vwx file as "<drawing> - <name>.csv" (or
in your home folder if the drawing has never been saved). Nothing in
the drawing is modified.

Simon L. Paige — AV Toolkit
License: MIT
"""

import csv
import os

try:
    import vs                       # only exists inside Vectorworks
except ImportError:                 # lets the file be imported for testing
    vs = None

TYPE_SYMBOL = 15
BASE_COLUMNS = ["Object Name", "Class", "Layer", "Symbol", "X", "Y"]


def _collect(criteria):
    handles = []
    vs.ForEachObject(handles.append, criteria)
    return handles


def _quote(name):
    return name.replace("'", "''")


def _field_names(record_handle):
    return [vs.GetFldName(record_handle, i) for i in range(1, vs.NumFields(record_handle) + 1)]


def inventory():
    """Count objects per attached record format and per plug-in object type."""
    records, plugins = {}, {}
    for h in _collect("ALL"):
        parametric = vs.GetParametricRecord(h)
        pio_name = vs.GetName(parametric) if parametric else ""
        if pio_name:
            plugins[pio_name] = plugins.get(pio_name, 0) + 1
        for i in range(1, vs.NumRecords(h) + 1):
            name = vs.GetName(vs.GetRecord(h, i))
            if name and name != pio_name:
                records[name] = records.get(name, 0) + 1
    return records, plugins


def find_objects(name):
    """Return (handles, field names) for a record format or plug-in object name."""
    handles = _collect("(R IN ['%s'])" % _quote(name))
    if handles:
        record = vs.GetObject(name)
        return handles, (_field_names(record) if record else [])
    handles = _collect("(PON='%s')" % _quote(name))
    if handles:
        return handles, _field_names(vs.GetParametricRecord(handles[0]))
    return [], []


def object_row(h, name, fields):
    layer = vs.GetLayer(h)
    x, y = vs.GetSymLoc(h)
    row = [vs.GetName(h), vs.GetClass(h), vs.GetLName(layer) if layer else "",
           vs.GetSymName(h) if vs.GetTypeN(h) == TYPE_SYMBOL else "",
           "%.3f" % x, "%.3f" % y]
    return row + [vs.GetRField(h, name, f) for f in fields]


def output_path(name):
    document = vs.GetFPathName()
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in name)
    if document and os.path.isdir(os.path.dirname(document)):
        base = os.path.splitext(document)[0]
    else:
        base = os.path.join(os.path.expanduser("~"), "Untitled")
    return "%s - %s.csv" % (base, safe)


def export(name):
    """Write the CSV. Returns (path, row count), or (None, 0) if nothing matched."""
    handles, fields = find_objects(name)
    if not handles:
        return None, 0
    path = output_path(name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(BASE_COLUMNS + fields)
        for h in handles:
            writer.writerow(object_row(h, name, fields))
    return path, len(handles)


def main():
    name = vs.StrDialog("Record format or plug-in object to export  ( ? = list what's in this drawing )", "?")
    if vs.DidCancel() or not name.strip():
        return
    name = name.strip()
    if name == "?":
        records, plugins = inventory()
        lines = ["Record formats:"] + ["   %s  (%d)" % kv for kv in sorted(records.items())]
        lines += ["", "Plug-in objects:"] + ["   %s  (%d)" % kv for kv in sorted(plugins.items())]
        if not records and not plugins:
            lines = ["No records or plug-in objects found in this drawing."]
        vs.AlrtDialog("\r".join(lines))
        return
    path, count = export(name)
    if path is None:
        vs.AlrtDialog("No objects found with a record or plug-in type named '%s'.\r"
                      "Run again and enter ? to see the names in this drawing." % name)
    else:
        vs.AlrtDialog("Exported %d object(s) to:\r%s" % (count, path))


if vs is not None:
    main()
