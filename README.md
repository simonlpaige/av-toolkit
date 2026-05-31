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

### Extron (SIS/Telnet)
| Script | What it does |
|--------|-------------|
| `extron/device-scanner.py` | Fast subnet scan for Extron devices via SIS. Reports model, firmware, part number. |
| `extron/config-backup.py` | Batch backup of Extron device configurations. Supports diff against previous backups. |
| `extron/sis-reference.md` | Quick reference for common SIS commands. |

### Q-SYS (Lua Plugins)
| Script | What it does |
|--------|-------------|
| `qsys/commissioning-check.lua` | Drop-in Q-SYS plugin that scans for muted outputs, extreme gain values, and no-signal paths. |
| `qsys/signal-tracer.lua` | Traces every signal path from named inputs through DSP chain to outputs. Flags broken paths. |
| `qsys/dsp-health.lua` | Continuous DSP health monitor with drift detection. Alerts on gain changes and mute state toggles. |

### Biamp (Tesira TTP)
| Script | What it does |
|--------|-------------|
| `biamp/tesira-status.py` | Poll Tesira units for identity, firmware, active VoIP sessions, and error conditions. |
| `biamp/config-diff.py` | Compare DSP configurations between identical rooms. Catches the "Room 4 sounds different" problem. |

### Crestron
| Script | What it does |
|--------|-------------|
| `crestron/iptable-checker.py` | Read Crestron IP tables, verify all entries are reachable, flag dead IP IDs and conflicts. |

### General / Multi-Manufacturer
| Script | What it does |
|--------|-------------|
| `general/av-net-scanner.cpp` | Fast multi-threaded TCP SYN scanner targeting AV-specific ports. Finds Q-SYS, Extron, Biamp, Crestron, Dante. |
| `general/cable-schedule.py` | Generate cable pull schedules from JSON. Outputs TSV, CSV, or Markdown for crew handoff. |
| `general/device-monitor.py` | Lightweight device uptime monitoring with webhook alerts (Slack, Telegram). Runs as a service. |

## Quick Start

```bash
# Find every Extron device on a subnet
python extron/device-scanner.py 192.168.1.0/24 --json

# Run a Q-SYS commissioning check (paste commissioning-check.lua
# into a Q-SYS Designer Custom Lua plugin)

# Poll a Biamp Tesira for status
python biamp/tesira-status.py 192.168.1.50

# Check Crestron IP table health
python crestron/iptable-checker.py 192.168.1.10

# Scan a whole building for AV gear
# (build with: g++ -std=c++17 -O2 general/av-net-scanner.cpp -o av-net-scanner -lws2_32)
av-net-scanner 10.0.1.0/24

# Generate a cable pull schedule
python general/cable-schedule.py my-project.json --format csv

# Monitor uptime on all room devices
python general/device-monitor.py devices.json --interval 60
```

## Requirements

- **Python 3.10+** — Most scripts. No dependencies beyond stdlib.
- **g++ (C++17)** — Only for `av-net-scanner.cpp`. Links against winsock on Windows, no deps on Linux.
- **Q-SYS Designer** — For the `.lua` plugins. These run inside Q-SYS Core processors, not standalone.
- **Network access** — Scripts need TCP reachability to the devices they manage.

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
