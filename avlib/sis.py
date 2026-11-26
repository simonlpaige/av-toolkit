"""
sis.py — Extron Simple Instruction Set (SIS) over TCP.

What an Extron unit does on a Telnet connection (port 23):

    (c) Copyright 2019, Extron Electronics, IN1608, V1.07, 60-1238-01
    Tue, 12 Feb 2019 13:46:08
    Password:                      <- only if a password is set

Simple SIS commands (I, N, Q, 3!) execute as soon as the characters
arrive — no terminator. Escape-sequence commands are ESC + text + CR.
Every response ends in CR LF. Errors come back as E01..E28.
"""

from __future__ import annotations

import re
import time

from .net import Session

ESC = "\x1b"

BANNER_RX = re.compile(
    r"Extron Electronics,\s*(?P<model>[^,\r\n]+?)\s*,\s*V(?P<firmware>[\w.\-]+)"
    r"(?:\s*,\s*(?P<part>\d{2}-\d{3,4}-\d{2,3}[A-Z]?))?")
PART_RX = re.compile(r"\b\d{2}-\d{3,4}-\d{2,3}[A-Z]?\b")
ERROR_RX = re.compile(r"^E(\d\d)$")

ERRORS = {
    "01": "invalid input number",
    "10": "invalid command",
    "11": "invalid preset number",
    "12": "invalid port/output number",
    "13": "invalid parameter",
    "14": "not valid for this configuration",
    "17": "invalid command for signal type",
    "22": "busy",
    "24": "privilege violation",
    "25": "device not present",
    "26": "maximum connections exceeded",
    "27": "invalid event number",
    "28": "bad filename or file not found",
}

# Read-only on every SIS product: identity and network settings.
IDENTITY_QUERIES = {
    "info": "I",
    "part_number": "N",
    "firmware": "Q",
    "firmware_build": "*Q",
    "model_name": "1I",
    "model_description": "2I",
    "unit_name": ESC + "CN\r",
    "mac_address": ESC + "CH\r",
    "ip_address": ESC + "CI\r",
    "subnet_mask": ESC + "CS\r",
    "gateway": ESC + "CG\r",
    "dhcp": ESC + "DH\r",
}


def describe_error(response: str) -> str | None:
    """'E13' -> 'E13 (invalid parameter)'; anything else -> None."""
    m = ERROR_RX.match(response.strip())
    if not m:
        return None
    return f"E{m.group(1)} ({ERRORS.get(m.group(1), 'unknown error')})"


def decode_command(text: str) -> str:
    r"""Turn a typed command into wire bytes: \e or {esc} -> ESC, \r or {cr} -> CR."""
    for token, char in ((r"\e", ESC), ("{esc}", ESC), ("{ESC}", ESC),
                        (r"\r", "\r"), ("{cr}", "\r"), ("{CR}", "\r")):
        text = text.replace(token, char)
    return text


def show_command(cmd: str) -> str:
    return cmd.replace(ESC, "{esc}").replace("\r", "{cr}")


class SISDevice:
    """An open SIS session. Use as a context manager."""

    def __init__(self, host: str, port: int = 23, timeout: float = 3.0,
                 password: str | None = None):
        self.host, self.port = host, port
        self.banner = ""
        self.model = self.firmware = self.part_number = ""
        self.password_required = False
        self.logged_in = False
        self.session = Session(host, port, timeout)
        try:
            self._handshake(password)
        except Exception:
            self.session.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def _handshake(self, password):
        # The banner arrives unprompted; most units finish within ~300 ms.
        text = self.session.read_idle(idle=0.4, timeout=min(self.session.timeout, 2.0))
        self.banner = text.strip()
        m = BANNER_RX.search(text)
        if m:
            self.model = m.group("model").strip()
            self.firmware = m.group("firmware")
            self.part_number = m.group("part") or ""
        if "password:" in text.lower():
            self.password_required = True
            if password is not None:
                self.session.send(password + "\r")
                reply, _ = self.session.read_until(r"Login \w+|Password:", timeout=self.session.timeout)
                self.logged_in = "login" in reply.lower()
        else:
            self.logged_in = True

    @property
    def is_extron(self) -> bool:
        return bool(self.model or self.part_number) or "extron" in self.banner.lower()

    def query(self, command: str, retries: int = 2) -> str:
        """Send one command, return its response text ('' if the unit stays silent)."""
        for attempt in range(retries + 1):
            self.session.read_idle(idle=0.02, timeout=0.05)   # drop anything unsolicited
            self.session.send(command)
            text, matched = self.session.read_until(r"\r\n")
            if matched:
                text += self.session.read_idle(idle=0.12, timeout=0.6)   # multi-line replies
            response = "\n".join(l.strip() for l in text.splitlines() if l.strip())
            if response == "E22" and attempt < retries:
                time.sleep(0.25)
                continue
            return response
        return response

    def identify(self):
        """Fill in model/firmware/part number by query when the banner didn't supply them."""
        if not self.part_number:
            m = PART_RX.search(self.query("N"))
            if m:
                self.part_number = m.group()
        if self.part_number and not self.firmware:
            fw = self.query("Q")
            if fw and not describe_error(fw):
                self.firmware = re.sub(r"^(Ver\d*\*?|Bld)", "", fw.splitlines()[0]).strip()
        if self.part_number and not self.model:
            model = self.query("1I")
            if model and not describe_error(model):
                self.model = re.sub(r"^Inf\d+\*", "", model.splitlines()[0]).strip()
