# Extron SIS Command Quick Reference

Extron devices use Simple Instruction Set (SIS) commands over Telnet or
RS-232. SIS is a family, not one command set: the commands below are the
common core, and **the product's user guide is the authority** for any
specific model. When in doubt, try it on a bench unit, not a live room.

## Connection

- Telnet: TCP port 23. On connect the unit sends a banner:
  `(c) Copyright 2019, Extron Electronics, IN1608, V1.07, 60-1238-01`
  followed by the date, then `Password:` if one is set.
- RS-232: 9600 baud, 8-N-1 by default.
- Ports 2001 and up on IPL / IPCP control processors are **serial
  pass-through** to whatever is wired to that COM port — anything you
  send there goes to the connected device, not to the Extron unit.

## Syntax

- Simple commands (`I`, `N`, `3!`) execute as the characters arrive. No
  terminator.
- Escape-sequence commands are `Esc` + text + `Return` (shown below as
  `{esc}` and `{cr}`). From a terminal, `W` can stand in for `Esc` and
  `|` for `Return`.
- Every response ends in CR LF.
- **A leading number usually turns a query into a set.** `Z` asks for
  the audio mute state; `1Z` mutes the audio.

## Error codes

| Code | Meaning |
|------|---------|
| `E01` | Invalid input number |
| `E10` | Invalid command |
| `E11` | Invalid preset number |
| `E12` | Invalid port / output number |
| `E13` | Invalid parameter |
| `E14` | Not valid for this configuration |
| `E17` | Invalid command for signal type |
| `E22` | Busy — retry after ~250 ms |
| `E24` | Privilege violation (not logged in as admin) |
| `E25` | Device not present |
| `E26` | Maximum connections exceeded |
| `E27` | Invalid event number |
| `E28` | Bad filename / file not found |

## Identity and network (read-only, safe everywhere)

| Command | Returns |
|---------|---------|
| `I` | Device information (format is model-specific) |
| `N` | Part number, e.g. `60-1238-01` |
| `Q` | Firmware version |
| `*Q` | Firmware version with build |
| `1I` | Model name |
| `2I` | Model description |
| `{esc}CN{cr}` | Unit name |
| `{esc}CH{cr}` | MAC address |
| `{esc}CI{cr}` | IP address |
| `{esc}CS{cr}` | Subnet mask |
| `{esc}CG{cr}` | Gateway |
| `{esc}DH{cr}` | DHCP mode (0 = off, 1 = on) |

These are what `config-backup.py` records by default.

## Switchers (IN, SW, MPS families — verify per model)

| Command | Action | Response |
|---------|--------|----------|
| `3!` | Select input 3 | `In3 All` |
| `!` | Query current input | input number |
| `45V` | Set volume | `Vol45` |
| `+V` / `-V` | Volume up / down | `Vol..` |
| `V` | Query volume | level |
| `1Z` / `0Z` | Audio mute on / off | `Amt1` / `Amt0` |
| `Z` | Query audio mute | `0` or `1` |
| `1B` / `0B` | Video mute on / off | `Vmt1` / `Vmt0` |
| `B` | Query video mute | `0` or `1` |
| `1X` / `0X` | Front panel lock on / off | `Exe1` / `Exe0` |
| `X` | Query front panel lock | `0` or `1` |

## Matrix switchers (CrossPoint, DXP, DTP CrossPoint)

| Command | Action | Response |
|---------|--------|----------|
| `1*2!` | Tie input 1 to output 2, audio + video | `Out02 In01 All` |
| `1*2%` | Tie video only | `Out02 In01 Vid` |
| `1*2$` | Tie audio only | `Out02 In01 Aud` |
| `2!` | Query what is tied to output 2 | input number |
| `3.` | Recall global preset 3 | `Rpr03` |
| `3,` | Save current ties as global preset 3 | `Spr03` |

## DSP (DMP family and DSP inside DTP CrossPoint / IN1608)

Gain and mute are addressed by object ID (OID); levels are in tenths of
a dB. OIDs are listed in each product's guide.

| Command | Action | Response |
|---------|--------|----------|
| `{esc}G40000*-100AU{cr}` | Set mic/line input 1 gain to -10.0 dB | `DsG40000*-100` |
| `{esc}G40000AU{cr}` | Query that gain | `-100` |
| `{esc}M40000*1AU{cr}` | Mute that input | `DsM40000*1` |
| `{esc}M40000AU{cr}` | Query that mute | `0` or `1` |

## Do not send casually

| Command | Effect |
|---------|--------|
| `{esc}ZXXX{cr}` | Reset to factory defaults |
| `{esc}ZQQQ{cr}` | Absolute reset, including IP settings |
| `{esc}1BOOT{cr}` | Reboot |

`config-backup.py --queries` refuses to send these.

## Notes for scripting

- Hold one connection open and wait for each response before sending
  the next command.
- Retry on `E22`.
- For a password-protected unit, send the password + `Return` at the
  `Password:` prompt and wait for `Login Administrator` or `Login User`.
- Verbose mode (`{esc}3CV{cr}`) makes the unit tag its responses and
  report front-panel changes unsolicited. It lasts for the session.
