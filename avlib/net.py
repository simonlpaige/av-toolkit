"""
net.py — Shared plumbing for the control-protocol scripts.

Target expansion (single host, CIDR, ranges, batch files), a persistent
line-oriented TCP session that survives telnet option negotiation, and a
small ordered thread-pool helper.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class TargetError(ValueError):
    pass


def expand_targets(target: str, max_hosts: int = 65536) -> list[str]:
    """
    '192.168.1.0/24'            -> every usable host in the subnet
    '192.168.1.10-50'           -> .10 through .50
    '192.168.1.10-192.168.2.5'  -> full range
    '192.168.1.10' / 'core-1'   -> itself
    """
    target = target.strip()
    try:
        if "/" in target:
            net = ipaddress.IPv4Network(target, strict=False)
            if net.num_addresses > max_hosts:
                raise TargetError(f"{target} is {net.num_addresses} addresses — too large to scan")
            return [str(ip) for ip in (net.hosts() if net.prefixlen < 31 else net)]
        m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)-(\d+(?:\.\d+\.\d+\.\d+)?)", target)
        if m:
            first = ipaddress.IPv4Address(m.group(1))
            tail = m.group(2)
            last = (ipaddress.IPv4Address(tail) if "." in tail
                    else ipaddress.IPv4Address(f"{m.group(1).rsplit('.', 1)[0]}.{tail}"))
            if int(last) < int(first):
                raise TargetError(f"range '{target}' ends before it starts")
            if int(last) - int(first) + 1 > max_hosts:
                raise TargetError(f"range '{target}' is too large to scan")
            return [str(ipaddress.IPv4Address(i)) for i in range(int(first), int(last) + 1)]
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as e:
        if isinstance(e, TargetError):
            raise
        raise TargetError(f"'{target}' is not a valid IP, range or subnet ({e})") from e
    if not target or " " in target:
        raise TargetError(f"'{target}' is not a valid host")
    return [target]


def read_targets_file(path: str) -> list[tuple[str, str]]:
    """
    Read a batch file: one device per line, '#' comments, optional label.

        192.168.1.50   Courtroom 3A DSP
        192.168.1.51,  Courtroom 3B DSP   # same design as 3A
    """
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = re.split(r"[\s,;]+", line, maxsplit=1)
            out.append((parts[0], parts[1].strip() if len(parts) > 1 else ""))
    return out


def run_parallel(func, items, workers: int = 16) -> list:
    """Run func over items concurrently; results come back in input order."""
    items = list(items)
    if not items:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        return list(pool.map(func, items))


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Session:
    """
    One TCP connection, kept open for a whole conversation.

    Telnet option negotiation (which Biamp and some Crestron/Extron units
    open with) is refused politely and stripped, so callers only see text.
    """

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = ""
        self._pending = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, text: str):
        self.sock.sendall(text.encode("latin-1", errors="replace"))

    def _strip_telnet(self, data: bytes) -> bytes:
        data = self._pending + data
        self._pending = b""
        out = bytearray()
        reply = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= len(data):
                self._pending = data[i:]
                break
            cmd = data[i + 1]
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(data):
                    self._pending = data[i:]
                    break
                opt = data[i + 2]
                if cmd == DO:
                    reply += bytes((IAC, WONT, opt))
                elif cmd == WILL:
                    reply += bytes((IAC, DONT, opt))
                i += 3
            elif cmd == SB:
                end = data.find(bytes((IAC, SE)), i)
                if end < 0:
                    self._pending = data[i:]
                    break
                i = end + 2
            elif cmd == IAC:
                out.append(IAC)
                i += 2
            else:
                i += 2
        if reply:
            try:
                self.sock.sendall(bytes(reply))
            except OSError:
                pass
        return bytes(out)

    def _recv(self, wait: float) -> bool:
        """Pull whatever arrives within `wait` seconds. False on silence or close."""
        self.sock.settimeout(max(wait, 0.01))
        try:
            chunk = self.sock.recv(4096)
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            return False
        if not chunk:
            return False
        self._buf += self._strip_telnet(chunk).decode("latin-1")
        return True

    def read_until(self, pattern: str, timeout: float | None = None) -> tuple[str, bool]:
        """
        Read until regex `pattern` matches the buffered text.
        Returns (text up to and including the match, matched?). On timeout
        everything received so far is returned with matched=False.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        rx = re.compile(pattern, re.M)
        while True:
            m = rx.search(self._buf)
            if m:
                text, self._buf = self._buf[:m.end()], self._buf[m.end():]
                return text, True
            left = deadline - time.monotonic()
            if left <= 0 or not self._recv(left):
                text, self._buf = self._buf, ""
                return text, False

    def read_idle(self, idle: float = 0.3, timeout: float | None = None) -> str:
        """Read until the line goes quiet for `idle` seconds (or timeout)."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            if not self._recv(min(idle, max(deadline - time.monotonic(), 0.01))):
                break
        text, self._buf = self._buf, ""
        return text
