# Vectorworks tools

Two ways to get design data out of a drawing and into the rest of the
toolkit.

## 1. Export a worksheet (no script needed)

Any Vectorworks worksheet — an equipment schedule, a ConnectCAD device
or cable report — can be exported as CSV, tab-delimited text or Excel.
The toolkit reads all three, including the UTF-16 text Vectorworks
writes on Windows.

## 2. `vw-export-records.py` (runs inside Vectorworks)

Exports every object that carries a given record format, or every
plug-in object of a given type, to a CSV beside the drawing: one row
per object, one column per field, plus name, class, layer, symbol and
position. It only reads the drawing.

Install: Resource Manager → New Resource → Script → language **Python
Script** → paste the file. Run it and enter `?` first — that lists the
record formats and plug-in object types in the drawing with a count, so
you can see what your device objects are actually called.

## What to do with the export

**Check the drawing against the D-Tools BOM**

```bash
python general/bom-reconcile.py dtools-bom.xlsx "Courthouse - Device.csv" --by-location
```

Reports equipment drawn but not priced, priced but not drawn, and
quantity mismatches. ConnectCAD's `Make` is read as manufacturer and
`Layer` / `Room` as location; use `--drawing-map location="..."` if your
rooms live in another field.

**Send what's new on the drawing to D-Tools**

```bash
python dtools/drawing-to-import.py "Courthouse - Device.csv" --only-missing-from dtools-bom.xlsx --out import.csv
```

Writes one line per part per location with quantities, containing only
what the drawing has beyond the current BOM, for the D-Tools SI import.

**Turn a ConnectCAD cable report into a pull schedule**

```bash
python general/cable-schedule.py circuits.csv --check
python general/cable-schedule.py circuits.csv --summary --slack 15 --spool 1000
python general/cable-schedule.py circuits.csv --format labels > labels.csv
```

Source/destination device and socket, cable type, signal, number and
length columns are matched by name. Bare lengths are read as feet
unless the header says metres (`Length (m)`) or you pass `--units m`.

## Try it

```bash
python general/bom-reconcile.py examples/dtools-bom-rev-a.csv examples/vectorworks-devices.csv \
    --ignore "*cable*" --drawing-map location=Room
python general/cable-schedule.py examples/connectcad-circuits.csv --check
```
