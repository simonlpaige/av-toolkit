# Extron SIS Command Quick Reference

Extron devices use Simple Instruction Set (SIS) commands over TCP (port 23)
or RS-232. Commands are ASCII, terminated with carriage return (`\r`).

## Connection
- TCP port 23 (Telnet) or 2001 on some models
- 9600-115200 baud (RS-232), 8-N-1
- Some devices also expose a web UI on port 80/443

## Response codes
- `E10` — Invalid command
- `E12` — Invalid port number
- `E13` — Invalid parameter
- `E22` — Busy
- `E24` — Privilege violation (wrong password)

## Common Query Commands

| Command | Returns | Example Response |
|---------|---------|-----------------|
| `I` | Info (model, firmware) | `VXX AV 01/15/24-10:30:00` |
| `N` | Part number | `60-1234-01` |
| `Q` | Full query | Model + firmware + part number |
| `0Z` | Volume level | `Vol050` (0-100) |
| `0G` | Global preset | `+01` (active preset number) |
| `1Z` | Audio input 1 level | `Aud001` (dB) |
| `1B` | Video input 1 status | `Vid001` |
| `1*1!` | Tie input 1 to output 1 | `Tie1*1!` |
| `0*1!` | Untie all outputs | `Out1 Untie` |

## Common Set Commands

| Command | Action | Example |
|---------|--------|---------|
| `0*50Z` | Set volume to 50 | `Vol050` |
| `1*2!` | Tie input 1 to output 2 | `Tie1*2!` |
| `1B` | Select input 1 | `In1 All` |
| `1*1*1*0AU` | Set input 1 audio gain to 0 dB | `In1 Aud=000` |
| `ZXXX` | Mute output | `Amt01*1` |
| `0*1Z` | Unmute output | `Amt01*0` |
| `0*1*GRPM` | Set group master | `GrpmD01*1` |
| `EXEC` | Execute a preset | `Rpr01` |

## Notes for scripting
- Wait for the response (`\r\n`) before sending the next command.
- The `Q` command returns a combined response useful for initial device fingerprinting.
- `E22` (device busy) can retry after 200ms.
- Password-protected devices require `"<password>"` prefix on commands.
- For video matrix switchers, use `I` first to determine I/O count before routing.
