# AV Toolkit — Commissioning, Automation, and Monitoring Scripts

Practical tools for audiovisual system integration and commissioning.
Built by a CTS-I/CTS-D field engineer who got tired of doing the same
checks by hand.

Every script here solves a real problem: finding devices on a subnet,
backing up configs, validating DSP signal paths, generating cable
schedules, monitoring uptime. Nothing here is hypothetical — these
all address tasks I've done on-site at courthouses, council chambers,
university theaters, and corporate AV deployments.

## What's Inside

### Design: D-Tools and Vectorworks
| Script | What it does |
|--------|-------------|
| `dtools/bom-audit.py` | Lint a D-Tools BOM export before it goes out: below-cost lines, $0 items, thin margin, TBD models, the same part priced two ways. Totals by location / system / phase. |
| `dtools/bom-diff.py` | Compare two BOM revisions. Added, removed, re-quantified and re-priced parts with the dollar impact — the raw material for a change order. |
| `dtools/rack-loads.py` | Watts, amps, BTU/hr, rack units and weight per rack from the BOM. Circuit count at 80% loading; flags racks that overflow. |
| `general/bom-reconcile.py` | D-Tools BOM vs Vectorworks equipment list. Finds gear that is drawn but not priced, priced but not drawn, or counted differently. |
| `dtools/companion-check.py` | Finds the accessory the BOM forgot: displays with no mounts, projectors with no screen, racks with no power distribution. Rules are editable. |
| `dtools/rack-elevation.py` | First-pass rack elevation from the BOM: U-numbered layout per rack, heaviest at the bottom, optional vent spacing, overflow check. |
| `dtools/drawing-to-import.py` | Vectorworks equipment list → D-Tools import CSV, rolled up per part per location. `--only-missing-from` writes just what the drawing has beyond the current BOM. |
| `general/ip-schedule.py` | `build` an IP schedule from the BOM's networked devices; `verify` it for duplicate addresses, subnet mistakes and devices that don't answer. |
| `vectorworks/vw-export-records.py` | Runs inside Vectorworks. Exports record / plug-in object data (ConnectCAD devices, equipment records) to CSV. |
| `general/cable-schedule.py` | Cable pull schedules from JSON, a ConnectCAD cable report or a D-Tools wire export. Schedule, label sheet, bulk takeoff with slack and spool counts, and a `--check` for duplicate IDs, dangling ends and over-length runs. |

All of these read `.csv`, tab-delimited text and `.xlsx` directly, match
column headers by name, and take `--map field="Column"` for anything
unusual. See [`dtools/README.md`](dtools/README.md) and
[`vectorworks/README.md`](vectorworks/README.md).

### Extron (SIS/Telnet)
| Script | What it does |
|--------|-------------|
| `extron/device-scanner.py` | Fast subnet scan for Extron devices. Reads model, firmware and part number from the connect banner; lists password-protected units too. |
| `extron/config-backup.py` | Snapshot device identity and network settings to JSON, diff against the previous backup. Model-specific state via `--queries`. |
| `extron/sis-reference.md` | Quick reference for common SIS commands. |

### Q-SYS
| Script | What it does |
|--------|-------------|
| `qsys/qrc-snapshot.py` | From a laptop, over QRC: snapshot every control on every named component, diff Core against Core or against a saved snapshot. |
| `qsys/commissioning-check.qplug` | Plugin. Scans named components for mutes, extreme gains, bypassed blocks and device faults. |
| `qsys/signal-tracer.qplug` | Plugin. Walks signal chains you define and reports the first muted, floored or faulted stage. |
| `qsys/dsp-health.qplug` | Plugin. Baselines mutes and gains, then alerts on drift, mute changes and device faults. |

### Biamp (Tesira TTP)
| Script | What it does |
|--------|-------------|
| `biamp/tesira-status.py` | Poll Tesira units for hostname, serial, firmware and active faults. Batch and monitor modes. |
| `biamp/config-diff.py` | Compare DSP block settings between identical rooms, or against a saved snapshot. Catches the "Room 4 sounds different" problem. |

### Crestron
| Script | What it does |
|--------|-------------|
| `crestron/iptable-checker.py` | Read a processor's IP table, flag offline entries, dead addresses, wrong-subnet entries and duplicate IP IDs. Also checks pasted console output. |

### General / Multi-Manufacturer
| Script | What it does |
|--------|-------------|
| `general/av-net-scanner.cpp` | Fast multi-threaded TCP connect scanner for AV ports. Groups results per host and identifies Extron, Biamp and Crestron units from their banners. |
| `general/mdns-discover.py` | Finds Dante devices (and NDI, AirPlay, Chromecast with `--all`) by mDNS — the gear a TCP port scan cannot see. |
| `general/commissioning-report.py` | Assembles the JSON output of every other tool into one Markdown handoff report with a pass/fail summary. |
| `general/device-monitor.py` | Lightweight uptime monitoring with webhook alerts and a CSV log. Device list from JSON, CSV or Excel. |

## Quick Start

```bash
# Is the BOM safe to send?
python dtools/bom-audit.py project-bom.xlsx --min-margin 25 --group-by location

# What changed since the last revision?
python dtools/bom-diff.py rev-A.xlsx rev-B.xlsx --format markdown

# Does the Vectorworks drawing match the D-Tools BOM?
python general/bom-reconcile.py project-bom.xlsx vectorworks-devices.csv --by-location

# Add what the drawing has that the BOM doesn't to D-Tools
python dtools/drawing-to-import.py vectorworks-devices.csv --only-missing-from project-bom.xlsx --out import.csv

# IP schedule from the BOM, then check it on site
python general/ip-schedule.py build project-bom.xlsx --subnet 192.168.10.0/24 --out ip-schedule.csv
python general/ip-schedule.py verify ip-schedule.csv

# Did the BOM forget the mounts, the screen or the rack power?
python dtools/companion-check.py project-bom.xlsx

# First-pass rack elevation
python dtools/rack-elevation.py project-bom.xlsx --only rack --vent-every 4

# Power, heat and rack space per rack
python dtools/rack-loads.py project-bom.xlsx --only rack --rack-ru 44

# Cable schedule from a ConnectCAD cable report — check it, then print it
python general/cable-schedule.py circuits.csv --check
python general/cable-schedule.py circuits.csv --format csv > schedule.csv

# Find every Extron device on a subnet
python extron/device-scanner.py 192.168.1.0/24 --json

# Snapshot a Q-SYS Core at handoff; diff it on the next service call
python qsys/qrc-snapshot.py 192.168.1.10 --save courtroom-3a.json
python qsys/qrc-snapshot.py courtroom-3a.json 192.168.1.10

# Poll a Biamp Tesira for faults
python biamp/tesira-status.py 192.168.1.50

# Check Crestron IP table health
python crestron/iptable-checker.py 192.168.1.10 --expect-subnet 192.168.1.0/24

# Scan a whole building for AV gear
g++ -std=c++17 -O2 -pthread general/av-net-scanner.cpp -o av-net-scanner   # add -lws2_32 on Windows
./av-net-scanner 10.0.1.0/24

# Find Dante devices (use --interface on a laptop with several adapters)
python general/mdns-discover.py --interface 192.168.1.50

# Monitor uptime on all room devices (the IP schedule works as the device list)
python general/device-monitor.py ip-schedule.csv --interval 60

# Roll every tool's --json output into one handoff report
python general/commissioning-report.py results/ --project "Courtroom 3A" --out report.md
```

Sample files to try everything against are in [`examples/`](examples/).

## Requirements

- **Python 3.10+** — No dependencies beyond the standard library (`.xlsx` included). Run scripts from a checkout of this repo: they share helpers in `avlib/`.
- **g++ (C++17)** — Only for `av-net-scanner.cpp`.
- **Q-SYS Designer** — For the `.qplug` plugins: copy them to `Documents/QSC/Q-Sys Designer/Plugins/`.
- **Vectorworks** — For `vw-export-records.py`, which runs as a Python script resource inside Vectorworks.
- **Network access** — The control scripts need TCP reachability to the devices they talk to.

## Safety

The control-protocol scripts only read. They send identity queries and
`get` commands, never a set, a preset recall or a reboot. Extron state
queries are opt-in (`--queries`) because on many switchers the command
that looks like a query — `1Z`, `1B` — is the one that mutes the room.

## Tests

```bash
pip install pytest
python -m pytest tests
```

The protocol scripts are tested against simulated devices on localhost
that speak each protocol as documented (SIS banner and password prompt,
Tesira Telnet negotiation and TTP, Crestron console, QRC JSON-RPC,
mDNS responses).

## Contributing

This toolkit grows as I encounter new commissioning pain points. If you
find a bug or want to add support for a platform I haven't covered —
Cisco, Poly, Shure, Sennheiser, AMX, whatever — open an issue or PR.

## License

MIT — use it, modify it, ship it. If these scripts save you a callback,
I've done my job.

---

**Simon L. Paige**
CTS, CTS-I, CTS-D · Q-SYS Level 2 · Extron Pro · Biamp · ETC
[simonlpaige.com/portfolio](https://simonlpaige.com/portfolio)
