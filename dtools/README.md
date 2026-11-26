# D-Tools tools

These read what D-Tools System Integrator exports — they never touch
the SI database or the project file.

| Script | Question it answers |
|--------|--------------------|
| `bom-audit.py` | Is this BOM safe to send? (below-cost lines, $0 items, thin margin, TBDs, inconsistent pricing) |
| `bom-diff.py` | What changed between revision A and B, and what is it worth? |
| `rack-loads.py` | How many circuits, how much heat, how many RU, how much weight per rack? |
| `drawing-to-import.py` | What does the Vectorworks drawing have that still needs adding to D-Tools? (writes an import CSV) |
| `../general/ip-schedule.py` | Build an IP schedule from the BOM's networked devices, then verify it |
| `../general/bom-reconcile.py` | Does the BOM match the Vectorworks drawing? |
| `../general/cable-schedule.py` | Turn a wire export into a pull schedule, label sheet and bulk takeoff |

## Getting data out of SI

Export any line-item view to **Excel (.xlsx) or CSV** — the product
grid, or a report that lists one product per row. Include whichever of
these columns you want checked:

| For | Columns |
|-----|---------|
| Everything | Manufacturer, Model, Quantity |
| Pricing checks | Unit Cost, Unit Price (or the extended versions) |
| Grouping | Location, System, Phase, Category |
| Labor check | Labor Hours |
| `rack-loads.py` | Watts and/or Amps, BTU, Rack Units, Weight |

Grouped reports work too: title rows above the header and subtotal rows
are skipped.

## Column names

Headers are matched by name, loosely — `Qty`, `Quantity` and `QTY`
are the same; so are `Mfr`, `Manufacturer` and `Brand`. If a column
isn't picked up, the error lists the columns in the file and you point
at the right one:

```bash
python dtools/bom-audit.py bom.xlsx --map model="Part #" --map unit_price="Client Price"
```

Fields you can map: `manufacturer`, `model`, `description`, `category`,
`phase`, `location`, `system`, `quantity`, `unit_cost`, `unit_price`,
`ext_cost`, `ext_price`, `labor_hours`, `watts`, `amps`, `btu`,
`rack_units`, `weight`, `component_id`.

## Try it

```bash
python dtools/bom-audit.py examples/dtools-bom-rev-a.csv --group-by location
python dtools/bom-diff.py examples/dtools-bom-rev-a.csv examples/dtools-bom-rev-b.csv
python dtools/rack-loads.py examples/dtools-bom-rev-a.csv --only rack --rack-ru 44
python dtools/drawing-to-import.py examples/vectorworks-devices.csv --only-missing-from examples/dtools-bom-rev-a.csv
python general/ip-schedule.py build examples/dtools-bom-rev-a.csv --subnet 192.168.10.0/24
```
