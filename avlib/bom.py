"""
bom.py — Load an equipment list (bill of materials) from any design tool.

One loader for D-Tools SI exports, Vectorworks / ConnectCAD equipment
worksheets, and hand-built spreadsheets. Column names are matched by
alias; anything unusual can be pointed at with --map field="Column".
"""

from __future__ import annotations

from dataclasses import dataclass

from .tabular import Table, load_table, norm_key, to_number

BOM_ALIASES = {
    "manufacturer": ["mfr", "mfg", "mfgr", "brand", "make", "manufacturer name", "vendor"],
    "model": ["model number", "model no", "model name", "part number", "part no", "part",
              "mfr part number", "sku", "item"],
    "description": ["short description", "desc", "item description", "product description"],
    "category": ["product category", "product type", "device type", "type"],
    "phase": ["install phase", "phase name"],
    "location": ["location name", "locations", "room", "room name", "area", "rack", "layer"],
    "system": ["system name", "systems", "subsystem"],
    "quantity": ["qty", "count", "total qty", "total quantity", "qty total"],
    "unit_cost": ["cost", "cost each", "unit cost each", "dealer cost", "our cost"],
    "unit_price": ["price", "sell price", "price each", "unit sell", "sell", "selling price"],
    "ext_cost": ["extended cost", "ext cost", "total cost", "cost total"],
    "ext_price": ["extended price", "ext price", "total price", "price total", "amount", "total"],
    "labor_hours": ["hours", "labor hrs", "install hours", "total labor hours", "total hours",
                    "labor", "unit labor hours"],
    "watts": ["power", "power w", "wattage", "power consumption"],
    "amps": ["amperage", "current", "amp draw"],
    "btu": ["btus", "btu hr", "heat", "heat load", "thermal"],
    "rack_units": ["ru", "rack unit", "rack spaces", "rack space", "rack height", "u height", "rus"],
    "weight": ["weight lbs", "weight lb", "unit weight", "shipping weight"],
    "component_id": ["component", "tag", "device tag", "device id", "name", "device name", "id"],
}

PLACEHOLDER_MODELS = {"", "TBD", "TBA", "MISC", "CUSTOM", "NA", "NONE", "UNKNOWN", "XXX", "TBC"}


@dataclass
class Item:
    row: int
    manufacturer: str = ""
    model: str = ""
    description: str = ""
    category: str = ""
    phase: str = ""
    location: str = ""
    system: str = ""
    component_id: str = ""
    quantity: float = 1.0
    unit_cost: float | None = None
    unit_price: float | None = None
    labor_hours: float | None = None
    watts: float | None = None
    amps: float | None = None
    btu: float | None = None
    rack_units: float | None = None
    weight: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (norm_key(self.manufacturer), norm_key(self.model))

    @property
    def ext_price(self) -> float:
        return (self.unit_price or 0.0) * self.quantity

    @property
    def ext_cost(self) -> float:
        return (self.unit_cost or 0.0) * self.quantity

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.manufacturer, self.model) if p) or self.description or "(blank)"


def load_bom(path, overrides=None, sheet=None, require_model=True) -> tuple[list[Item], Table]:
    """
    Load a BOM. Subtotal rows (no manufacturer and no model) are dropped.
    A missing Quantity column means one row = one unit, as in a drawing's
    device list. Unit values are derived from extended ones if need be.
    """
    table = load_table(path, BOM_ALIASES, required=["model"] if require_model else [],
                       overrides=overrides, sheet=sheet)
    items = []
    for r in table.rows:
        manufacturer, model = r.get("manufacturer", ""), r.get("model", "")
        if not manufacturer and not model:
            continue
        if table.has("quantity"):
            if not r["quantity"]:
                continue                    # group header / spacer row
            qty = to_number(r["quantity"], 0.0)
        else:
            qty = 1.0
        item = Item(row=r["_row"], manufacturer=manufacturer, model=model, quantity=qty,
                    **{f: r.get(f, "") for f in ("description", "category", "phase", "location",
                                                 "system", "component_id")})
        for f in ("unit_cost", "unit_price", "labor_hours", "watts", "amps", "btu",
                  "rack_units", "weight"):
            if table.has(f):
                setattr(item, f, to_number(r[f], None))
        for unit, ext in (("unit_cost", "ext_cost"), ("unit_price", "ext_price")):
            if getattr(item, unit) is None and table.has(ext) and qty:
                total = to_number(r[ext], None)
                if total is not None:
                    setattr(item, unit, total / qty)
        items.append(item)
    return items, table


def add_table_args(parser):
    """The --map / --sheet options every spreadsheet-reading tool shares."""
    parser.add_argument("--map", action="append", default=[], metavar='FIELD="Column"',
                        help="Use this column for a field, e.g. --map model=\"Part #\" (repeatable)")
    parser.add_argument("--sheet", help="Worksheet name for .xlsx files (default: first sheet)")
