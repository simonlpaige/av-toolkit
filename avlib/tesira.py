"""
tesira.py — Biamp Tesira Text Protocol (TTP) over Telnet.

A Tesira server opens port 23 with Telnet option negotiation, then:

    Welcome to the Tesira Text Protocol Server...

Commands are "<InstanceTag> <verb> <attribute> [indexes] [value]" + LF.
Telnet echoes the command, then one line comes back:

    +OK "value":-10.000000
    +OK "list":["Level1" "Mixer1"]
    -ERR address not found: {"deviceId":0 "classCode":0 "instanceNum":0}

Only `get` is ever sent from here.
"""

from __future__ import annotations

import re

from .net import Session

REPLY_RX = r"^(\+OK|-ERR|-CANNOT_DELIVER|-GENERAL_FAILURE)[^\r\n]*\r?\n"


class TesiraError(Exception):
    pass


def parse_value(payload: str):
    """'"value":-10.000000' -> -10.0, '"value":"3.14"' -> '3.14', lists -> list of str."""
    payload = payload.strip()
    m = re.match(r'"(value|list)":(.*)$', payload, re.S)
    if not m:
        return payload
    kind, body = m.group(1), m.group(2).strip()
    if body.startswith("[") and (kind == "list" or not body.startswith("[{")):
        return re.findall(r'"((?:[^"\\]|\\.)*)"', body) or body.strip("[]").split()
    if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
        return body[1:-1]
    if body in ("true", "false"):
        return body == "true"
    try:
        return float(body) if re.search(r"[.eE]", body) else int(body)
    except ValueError:
        return body


class Tesira:
    """An open TTP session. Use as a context manager."""

    def __init__(self, host: str, port: int = 23, timeout: float = 3.0):
        self.host, self.port = host, port
        self.session = Session(host, port, timeout)
        text, self.welcomed = self.session.read_until(r"Tesira Text Protocol Server[^\r\n]*\r?\n",
                                                      timeout=timeout)
        self.banner = text.strip()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def raw(self, command: str) -> str:
        """Send a command, return the reply line ('+OK ...' / '-ERR ...')."""
        self.session.send(command + "\n")
        while True:
            text, matched = self.session.read_until(REPLY_RX)
            if not matched:
                raise TesiraError(f"no reply to '{command}'"
                                  + ("" if self.welcomed else " (no TTP welcome banner either — "
                                                              "is this a Tesira, and is Telnet enabled?)"))
            return text.strip().splitlines()[-1].strip()

    def get(self, tag: str, attribute: str, *indexes):
        """Return the parsed value, or raise TesiraError on -ERR."""
        command = " ".join([tag, "get", attribute, *map(str, indexes)])
        reply = self.raw(command)
        if not reply.startswith("+OK"):
            raise TesiraError(reply)
        return parse_value(reply[3:])

    def try_get(self, tag: str, attribute: str, *indexes, default=None):
        try:
            return self.get(tag, attribute, *indexes)
        except TesiraError:
            return default


def fault_names(active_fault_list) -> list[str]:
    """Pull human-readable fault names out of a DEVICE activeFaultList reply."""
    text = active_fault_list if isinstance(active_fault_list, str) else " ".join(map(str, active_fault_list))
    names = re.findall(r'"name":"([^"]*)"', text)
    return [n for n in names if n and not n.lower().startswith("no fault")]
