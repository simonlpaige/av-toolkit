// av-net-scanner.cpp — Fast AV device discovery on local subnets.
//
// A TCP connect() scanner (full three-way handshake, NOT a raw SYN scan, so
// it needs no privileges) that probes only the ports AV gear actually
// listens on, then groups what it finds per host and makes a best-effort
// vendor guess. All ports of one host are probed concurrently, so a dead
// host costs one timeout rather than one timeout per port.
//
// Limitations:
//   * TCP only. Devices that are discoverable only via UDP/mDNS are NOT
//     seen. That notably includes most Dante devices (discovery and
//     control are mDNS/UDP), so no Dante port is in the table.
//   * Port numbers are vendor defaults; a hit on a non-exclusive port
//     (23, 80, 1024, 5000, ...) is a hint, not an identification.
//
// Default port table (see AV_PORTS below, the single source of truth):
//   22    SSH                        1710  Q-SYS QRC external control
//   23    Telnet (Extron SIS, Biamp  2202  Shure command strings
//         TTP, Crestron console...)  4352  PJLink
//   80    HTTP web UI                5000  Kramer Protocol 3000
//   443   HTTPS web UI               6107  Lightware LW3
//   1024  Panasonic LAN control      8080  HTTP alternate web UI
//   1319  AMX ICSP                   9761  LG display control
//   1515  Samsung MDC                41794 Crestron CIP
//   1702  Q-SYS ECP (legacy ASCII    41795 Crestron CTP console
//         external control)          41796 Crestron secure CIP
//   53595 Sony ADCP                  41797 Crestron secure CTP
//
// Banner identification (on by default, --no-banner to disable): on open
// ports 23 and 41795, and on any --ports port that is not in the table, the
// scanner reads what the device volunteers for up to --timeout ms and
// recognises Extron SIS, Biamp Tesira Text Protocol and Crestron console
// prompts. It never sends credentials or commands. The only bytes it sends
// are telnet option refusals (IAC WONT/DONT, needed before a Tesira prints
// its welcome line) and, on ports 23/41795 only, one bare CR to elicit a
// prompt from a silent console.
//
// Build:
//   Linux/macOS:  g++ -std=c++17 -O2 -pthread av-net-scanner.cpp -o av-net-scanner
//   Windows:      g++ -std=c++17 -O2 av-net-scanner.cpp -o av-net-scanner.exe -lws2_32
//
// Usage:
//   av-net-scanner 192.168.1.0/24
//   av-net-scanner 10.0.0.1-254 --workers 200
//   av-net-scanner --json 192.168.1.0/24 > devices.json
//   av-net-scanner 192.168.1.50 --ports 23,1710,41795 --timeout 1000
//
// Exit codes: 0 = at least one open port found, 1 = nothing found,
//             2 = usage error.
//
// Only scan networks you are authorised to scan.
//
// Simon L. Paige — AV Toolkit
// License: MIT

#ifdef _WIN32
  #ifndef _WIN32_WINNT
    #define _WIN32_WINNT 0x0600   // WSAPoll needs Vista or later
  #endif
  #ifndef NOMINMAX
    #define NOMINMAX
  #endif
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #include <io.h>
  #ifdef _MSC_VER
    #pragma comment(lib, "ws2_32.lib")
  #endif
#else
  #include <sys/types.h>
  #include <sys/socket.h>
  #include <sys/resource.h>
  #include <netinet/in.h>
  #include <poll.h>
  #include <unistd.h>
  #include <fcntl.h>
  #include <csignal>
#endif

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// ---------------------------------------------------------------------------
// Platform shims
// ---------------------------------------------------------------------------

#ifdef _WIN32
  using socket_t = SOCKET;
  using pollfd_t = WSAPOLLFD;
  static const socket_t BAD_SOCKET = INVALID_SOCKET;
  static int  sock_poll(pollfd_t* fds, size_t n, int ms) { return WSAPoll(fds, static_cast<ULONG>(n), ms); }
  static void sock_close(socket_t s) { closesocket(s); }
  static int  sock_errno() { return WSAGetLastError(); }
  static bool connect_in_progress(int e) { return e == WSAEWOULDBLOCK || e == WSAEINPROGRESS; }
  static bool poll_interrupted(int e) { return e == WSAEINTR; }
  static bool set_nonblocking(socket_t s) { u_long on = 1; return ioctlsocket(s, FIONBIO, &on) == 0; }
  static bool stderr_is_tty() { return _isatty(_fileno(stderr)) != 0; }
  static const int SEND_FLAGS = 0;
#else
  using socket_t = int;
  using pollfd_t = struct pollfd;
  static const socket_t BAD_SOCKET = -1;
  static int  sock_poll(pollfd_t* fds, size_t n, int ms) { return ::poll(fds, static_cast<nfds_t>(n), ms); }
  static void sock_close(socket_t s) { ::close(s); }
  static int  sock_errno() { return errno; }
  static bool connect_in_progress(int e) { return e == EINPROGRESS || e == EINTR; }
  static bool poll_interrupted(int e) { return e == EINTR; }
  static bool set_nonblocking(socket_t s) {
      int flags = fcntl(s, F_GETFL, 0);
      return flags >= 0 && fcntl(s, F_SETFL, flags | O_NONBLOCK) == 0;
  }
  static bool stderr_is_tty() { return isatty(STDERR_FILENO) != 0; }
  #ifdef MSG_NOSIGNAL
    static const int SEND_FLAGS = MSG_NOSIGNAL;
  #else
    static const int SEND_FLAGS = 0;
  #endif
#endif

using Clock = std::chrono::steady_clock;

static int ms_until(Clock::time_point t) {
    auto d = std::chrono::duration_cast<std::chrono::milliseconds>(t - Clock::now()).count();
    return d < 0 ? 0 : static_cast<int>(d);
}

// ---------------------------------------------------------------------------
// Port table
// ---------------------------------------------------------------------------

struct PortDef {
    uint16_t    port;
    const char* service;  // what normally listens here
    const char* vendor;   // likely vendor; generic where the port is shared
    const char* unique;   // host-level guess if this port alone is telling, else ""
    bool        banner;   // read a banner / prompt when open
};

static const PortDef AV_PORTS[] = {
    {22,    "SSH",                          "Various",                                   "",            false},
    {23,    "Telnet",                       "Extron SIS / Biamp TTP / Crestron / various", "",          true },
    {80,    "HTTP web UI",                  "Various",                                   "",            false},
    {443,   "HTTPS web UI",                 "Various",                                   "",            false},
    {1024,  "Panasonic LAN control",        "Panasonic projector/display (likely)",      "",            false},
    {1319,  "AMX ICSP",                     "AMX (Harman)",                              "AMX",         false},
    {1515,  "Samsung MDC",                  "Samsung display",                           "Samsung",     false},
    {1702,  "Q-SYS ECP (legacy control)",   "QSC Q-SYS",                                 "QSC Q-SYS",   false},
    {1710,  "Q-SYS QRC external control",   "QSC Q-SYS",                                 "QSC Q-SYS",   false},
    {2202,  "Shure command strings",        "Shure",                                     "Shure",       false},
    {4352,  "PJLink",                       "Projector/display (PJLink)",                "",            false},
    {5000,  "Kramer Protocol 3000",         "Kramer (likely; port is widely used)",      "",            false},
    {6107,  "Lightware LW3",                "Lightware",                                 "Lightware",   false},
    {8080,  "HTTP alternate web UI",        "Various",                                   "",            false},
    {9761,  "LG display control",           "LG display",                                "LG",          false},
    {41794, "Crestron CIP",                 "Crestron",                                  "Crestron",    false},
    {41795, "Crestron CTP console",         "Crestron",                                  "Crestron",    true },
    {41796, "Crestron secure CIP",          "Crestron",                                  "Crestron",    false},
    {41797, "Crestron secure CTP",          "Crestron",                                  "Crestron",    false},
    {53595, "Sony ADCP",                    "Sony projector",                            "Sony",        false},
};

static const PortDef* find_port_def(uint16_t port) {
    for (const auto& d : AV_PORTS)
        if (d.port == port) return &d;
    return nullptr;
}

// ---------------------------------------------------------------------------
// Strict parsing helpers (never throw)
// ---------------------------------------------------------------------------

static bool parse_uint(const std::string& s, uint64_t max, uint64_t& out) {
    if (s.empty() || s.size() > 10) return false;
    uint64_t v = 0;
    for (char c : s) {
        if (c < '0' || c > '9') return false;
        v = v * 10 + static_cast<uint64_t>(c - '0');
    }
    if (v > max) return false;
    out = v;
    return true;
}

// Dotted quad, four decimal octets, nothing else. Host byte order.
static bool parse_ipv4(const std::string& s, uint32_t& out) {
    uint32_t ip = 0;
    size_t pos = 0;
    for (int i = 0; i < 4; ++i) {
        size_t end = (i < 3) ? s.find('.', pos) : s.size();
        if (end == std::string::npos) return false;
        std::string part = s.substr(pos, end - pos);
        uint64_t v = 0;
        if (part.size() > 3 || !parse_uint(part, 255, v)) return false;
        ip = (ip << 8) | static_cast<uint32_t>(v);
        pos = end + 1;
    }
    out = ip;
    return true;
}

static std::string format_ipv4(uint32_t ip) {
    return std::to_string((ip >> 24) & 0xFF) + "." + std::to_string((ip >> 16) & 0xFF) + "." +
           std::to_string((ip >> 8) & 0xFF) + "." + std::to_string(ip & 0xFF);
}

struct Target {
    uint32_t first = 0;   // host byte order
    uint64_t count = 0;   // number of consecutive addresses
};

// Accepts a.b.c.d | a.b.c.d/prefix | a.b.c.d-N (last octet) | a.b.c.d-e.f.g.h
static bool parse_target(const std::string& text, Target& t, std::string& err) {
    auto slash = text.find('/');
    auto dash = text.find('-');
    if (slash != std::string::npos) {
        uint32_t base = 0;
        uint64_t prefix = 0;
        if (!parse_ipv4(text.substr(0, slash), base)) {
            err = "invalid IPv4 address in '" + text + "'";
            return false;
        }
        if (!parse_uint(text.substr(slash + 1), 32, prefix)) {
            err = "invalid CIDR prefix in '" + text + "' (expected 0-32)";
            return false;
        }
        uint32_t mask = (prefix == 0) ? 0u : (0xFFFFFFFFu << (32 - static_cast<unsigned>(prefix)));
        uint32_t network = base & mask;
        uint64_t size = uint64_t{1} << (32 - static_cast<unsigned>(prefix));
        if (prefix >= 31) {          // /31 point-to-point and /32 host: every address is usable
            t.first = network;
            t.count = size;
        } else {                     // skip network and broadcast addresses
            t.first = network + 1;
            t.count = size - 2;
        }
        return true;
    }
    if (dash != std::string::npos) {
        std::string lhs = text.substr(0, dash), rhs = text.substr(dash + 1);
        uint32_t start = 0, end = 0;
        if (!parse_ipv4(lhs, start)) {
            err = "invalid start address in range '" + text + "'";
            return false;
        }
        if (rhs.find('.') != std::string::npos) {
            if (!parse_ipv4(rhs, end)) {
                err = "invalid end address in range '" + text + "'";
                return false;
            }
        } else {
            uint64_t last = 0;
            if (!parse_uint(rhs, 255, last)) {
                err = "invalid range end in '" + text + "' (expected last octet 0-255 or a full address)";
                return false;
            }
            end = (start & 0xFFFFFF00u) | static_cast<uint32_t>(last);
        }
        if (end < start) {
            err = "range end is below range start in '" + text + "'";
            return false;
        }
        t.first = start;
        t.count = uint64_t{end} - start + 1;
        return true;
    }
    if (!parse_ipv4(text, t.first)) {
        err = "invalid target '" + text + "' (expected IP, a.b.c.d-N range, or CIDR)";
        return false;
    }
    t.count = 1;
    return true;
}

static bool parse_port_list(const std::string& text, std::vector<uint16_t>& ports, std::string& err) {
    ports.clear();
    size_t pos = 0;
    while (true) {
        size_t comma = text.find(',', pos);
        std::string item = text.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos);
        uint64_t v = 0;
        if (!parse_uint(item, 65535, v) || v == 0) {
            err = "invalid port '" + item + "' in --ports (expected comma-separated 1-65535)";
            return false;
        }
        ports.push_back(static_cast<uint16_t>(v));
        if (comma == std::string::npos) break;
        pos = comma + 1;
    }
    std::sort(ports.begin(), ports.end());
    ports.erase(std::unique(ports.begin(), ports.end()), ports.end());
    return true;
}

// ---------------------------------------------------------------------------
// Banner reading and classification
// ---------------------------------------------------------------------------

struct Ident {
    std::string vendor;   // empty = not identified
    std::string info;
    bool definite = false;  // no point reading further
};

static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\n");
    return s.substr(a, b - a + 1);
}

// Strips telnet IAC sequences and non-printables from `raw`. CR/LF become
// '\n'. Refusals for every DO/WILL request are appended to `replies`
// (DO x -> WONT x, WILL x -> DONT x). An incomplete trailing sequence is
// left for the next call (the whole buffer is re-parsed each time).
static std::string telnet_clean(const std::string& raw, std::string& replies) {
    std::string text;
    replies.clear();
    const size_t n = raw.size();
    size_t i = 0;
    while (i < n) {
        unsigned char c = static_cast<unsigned char>(raw[i]);
        if (c != 0xFF) {
            if (c >= 0x20 && c <= 0x7E) text += static_cast<char>(c);
            else if (c == '\n' || c == '\r') text += '\n';
            ++i;
            continue;
        }
        if (i + 1 >= n) break;
        unsigned char cmd = static_cast<unsigned char>(raw[i + 1]);
        if (cmd >= 251 && cmd <= 254) {              // WILL / WONT / DO / DONT + option
            if (i + 2 >= n) break;
            if (cmd == 253) { replies += "\xFF\xFC"; replies += raw[i + 2]; }   // DO   -> WONT
            if (cmd == 251) { replies += "\xFF\xFE"; replies += raw[i + 2]; }   // WILL -> DONT
            i += 3;
        } else if (cmd == 250) {                     // SB ... IAC SE
            size_t j = i + 2;
            bool closed = false;
            while (j + 1 < n) {
                if (static_cast<unsigned char>(raw[j]) == 0xFF &&
                    static_cast<unsigned char>(raw[j + 1]) == 240) { closed = true; break; }
                ++j;
            }
            if (!closed) break;
            i = j + 2;
        } else {
            i += 2;                                  // IAC IAC or a two-byte command
        }
    }
    return text;
}

static Ident classify_banner(const std::string& text) {
    Ident id;

    // "(c) Copyright 2019, Extron Electronics, DTP CrossPoint 84, V1.07, 60-1381-01"
    static const char EXTRON[] = "Extron Electronics";
    size_t pos = text.find(EXTRON);
    if (pos != std::string::npos) {
        size_t start = pos + sizeof(EXTRON) - 1;
        size_t eol = text.find('\n', start);
        std::string rest = text.substr(start, eol == std::string::npos ? std::string::npos : eol - start);
        size_t a = rest.find_first_not_of(", \t");
        id.vendor = "Extron";
        id.info = (a == std::string::npos) ? "" : trim(rest.substr(a));
        id.definite = (eol != std::string::npos);
        return id;
    }

    // "Welcome to the Tesira Text Protocol Server..."
    pos = text.find("Tesira Text Protocol");
    if (pos != std::string::npos) {
        size_t bol = text.rfind('\n', pos);
        bol = (bol == std::string::npos) ? 0 : bol + 1;
        size_t eol = text.find('\n', pos);
        id.vendor = "Biamp Tesira";
        id.info = trim(text.substr(bol, eol == std::string::npos ? std::string::npos : eol - bol));
        id.definite = true;
        return id;
    }

    // Crestron console prompt: the buffer ends in "<MODEL>" + ">" where MODEL
    // looks like CP4, MC4, DMPS3-4K-350-C, TSW-1070 ... Best-effort only.
    std::string t = trim(text);
    if (t.size() >= 3 && t.back() == '>') {
        size_t b = t.find_last_of(" \t\n", t.size() - 2);
        std::string token = t.substr(b == std::string::npos ? 0 : b + 1);
        token.pop_back();
        bool ok = token.size() >= 2 && token.size() <= 32 && token[0] >= 'A' && token[0] <= 'Z';
        bool digit = false;
        for (char c : token) {
            if (c >= '0' && c <= '9') digit = true;
            else if (!((c >= 'A' && c <= 'Z') || c == '-' || c == '_')) ok = false;
        }
        if (ok && digit) {
            id.vendor = "Crestron";
            id.info = token + " (console prompt, best-effort)";
            return id;
        }
    }

    // Unrecognised: keep the first line the device said as a hint.
    size_t a = text.find_first_not_of(" \t\n");
    if (a != std::string::npos) {
        size_t eol = text.find('\n', a);
        id.info = trim(text.substr(a, eol == std::string::npos ? std::string::npos : eol - a));
        if (id.info.size() > 80) id.info = id.info.substr(0, 77) + "...";
    }
    return id;
}

// Reads what the peer volunteers on an already-connected non-blocking socket
// for at most timeout_ms. If `nudge` is set and nothing printable has arrived
// after a short wait, sends a single CR to elicit a prompt.
static Ident grab_banner(socket_t s, int timeout_ms, bool nudge) {
    const int QUIET_MS = 200;        // stop once the device has gone quiet
    const size_t MAX_RAW = 4096;
    const auto start = Clock::now();
    const auto deadline = start + std::chrono::milliseconds(timeout_ms);
    const auto nudge_at = start + std::chrono::milliseconds(std::min(timeout_ms / 3, 300));

    std::string raw, text;
    size_t replied = 0;              // bytes of negotiation replies already sent
    bool nudged = !nudge;
    Clock::time_point last_text{};
    Ident id;

    while (Clock::now() < deadline && raw.size() < MAX_RAW) {
        int wait = ms_until(deadline);
        if (!text.empty()) {
            int quiet_left = ms_until(last_text + std::chrono::milliseconds(QUIET_MS));
            if (quiet_left == 0) break;
            wait = std::min(wait, quiet_left);
        } else if (!nudged) {
            wait = std::min(wait, ms_until(nudge_at));
        }

        pollfd_t pfd{};
        pfd.fd = s;
        pfd.events = POLLIN;
        int r = sock_poll(&pfd, 1, wait);
        if (r < 0) {
            if (poll_interrupted(sock_errno())) continue;
            break;
        }
        if (r == 0) {
            if (text.empty() && !nudged && Clock::now() >= nudge_at) {
                send(s, "\r", 1, SEND_FLAGS);
                nudged = true;
            }
            continue;
        }

        char buf[1024];
        auto got = recv(s, buf, sizeof(buf), 0);
        if (got <= 0) break;         // closed or error
        raw.append(buf, static_cast<size_t>(got));

        std::string replies;
        std::string now_text = telnet_clean(raw, replies);
        if (replies.size() > replied) {
            send(s, replies.data() + replied, static_cast<int>(replies.size() - replied), SEND_FLAGS);
            replied = replies.size();
        }
        if (now_text.find_first_not_of(" \t\n") != std::string::npos) {
            if (now_text != text) last_text = Clock::now();
            text = now_text;
            id = classify_banner(text);
            if (id.definite) break;
        }
    }
    if (!text.empty()) id = classify_banner(text);
    return id;
}

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------

struct ScanPort {
    uint16_t    port;
    std::string service;
    std::string vendor;   // table vendor ("" for unknown ports)
    std::string unique;
    bool        banner;   // eligible for banner read
    bool        nudge;    // may send a bare CR
};

struct OpenPort {
    uint16_t    port;
    std::string service;
    std::string vendor;
    std::string info;
    bool        identified = false;  // vendor came from a banner
    std::string unique;
};

struct HostResult {
    uint32_t              ip = 0;
    std::vector<OpenPort> ports;
    std::string           guess;
};

static const size_t PORT_BATCH = 32;   // sockets one worker holds open at once

// Connects to ports[begin, end) of one host concurrently. Open ports are
// appended to `out`.
static void scan_batch(uint32_t ip, const std::vector<ScanPort>& ports, size_t begin, size_t end,
                       int timeout_ms, bool banners, std::vector<OpenPort>& out) {
    struct Pending { socket_t sock; size_t idx; };
    std::vector<Pending> pending;
    std::vector<Pending> open;

    for (size_t i = begin; i < end; ++i) {
        socket_t s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (s == BAD_SOCKET) continue;
        if (!set_nonblocking(s)) { sock_close(s); continue; }

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(ports[i].port);
        addr.sin_addr.s_addr = htonl(ip);

        int rc = connect(s, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr));
        if (rc == 0) open.push_back({s, i});                              // connected at once
        else if (connect_in_progress(sock_errno())) pending.push_back({s, i});
        else sock_close(s);                                               // refused / unreachable
    }

    const auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
    std::vector<pollfd_t> pfds;
    while (!pending.empty()) {
        int wait = ms_until(deadline);
        if (wait == 0) break;
        pfds.assign(pending.size(), pollfd_t{});
        for (size_t k = 0; k < pending.size(); ++k) {
            pfds[k].fd = pending[k].sock;
            pfds[k].events = POLLOUT;
        }
        int r = sock_poll(pfds.data(), pfds.size(), wait);
        if (r < 0) {
            if (poll_interrupted(sock_errno())) continue;
            break;
        }
        if (r == 0) break;

        std::vector<Pending> still;
        for (size_t k = 0; k < pending.size(); ++k) {
            if (pfds[k].revents == 0) { still.push_back(pending[k]); continue; }
            int so_error = 0;
            socklen_t len = sizeof(so_error);
            int rc = getsockopt(pending[k].sock, SOL_SOCKET, SO_ERROR,
                                reinterpret_cast<char*>(&so_error), &len);
            bool ok = rc == 0 && so_error == 0 && (pfds[k].revents & POLLOUT) &&
                      !(pfds[k].revents & (POLLERR | POLLNVAL));
            if (ok) open.push_back(pending[k]);
            else sock_close(pending[k].sock);
        }
        pending.swap(still);
    }
    for (const auto& p : pending) sock_close(p.sock);   // timed out (filtered or no host)

    std::sort(open.begin(), open.end(), [](const Pending& a, const Pending& b) { return a.idx < b.idx; });
    for (const auto& o : open) {
        const ScanPort& sp = ports[o.idx];
        OpenPort op;
        op.port = sp.port;
        op.service = sp.service;
        op.vendor = sp.vendor;
        op.unique = sp.unique;
        if (banners && sp.banner) {
            Ident id = grab_banner(o.sock, timeout_ms, sp.nudge);
            op.info = id.info;
            if (!id.vendor.empty()) {
                op.vendor = id.vendor;
                op.identified = true;
            }
        }
        sock_close(o.sock);
        out.push_back(std::move(op));
    }
}

static HostResult scan_host(uint32_t ip, const std::vector<ScanPort>& ports, int timeout_ms, bool banners) {
    HostResult h;
    h.ip = ip;
    for (size_t b = 0; b < ports.size(); b += PORT_BATCH)
        scan_batch(ip, ports, b, std::min(ports.size(), b + PORT_BATCH), timeout_ms, banners, h.ports);

    // Host guess: a banner identification wins, then a vendor-unique port.
    for (const auto& p : h.ports)
        if (p.identified) { h.guess = p.vendor; return h; }
    for (const auto& p : h.ports)
        if (!p.unique.empty()) { h.guess = p.unique; return h; }
    return h;
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

static std::string json_escape(const std::string& s) {
    std::string out;
    for (char ch : s) {
        unsigned char c = static_cast<unsigned char>(ch);
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20 || c == 0x7F) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += ch;
                }
        }
    }
    return out;
}

static void print_json(const std::vector<HostResult>& hosts) {
    std::cout << "[";
    for (size_t i = 0; i < hosts.size(); ++i) {
        const auto& h = hosts[i];
        std::cout << (i ? ",\n" : "\n") << "  {\"ip\":\"" << format_ipv4(h.ip) << "\",\"ports\":[";
        for (size_t k = 0; k < h.ports.size(); ++k) {
            const auto& p = h.ports[k];
            std::cout << (k ? ",\n" : "\n")
                      << "    {\"port\":" << p.port
                      << ",\"service\":\"" << json_escape(p.service)
                      << "\",\"vendor\":\"" << json_escape(p.vendor)
                      << "\",\"info\":\"" << json_escape(p.info) << "\"}";
        }
        std::cout << "\n  ],\"guess\":\"" << json_escape(h.guess) << "\"}";
    }
    std::cout << (hosts.empty() ? "]\n" : "\n]\n");
}

static void print_text(const std::vector<HostResult>& hosts) {
    if (hosts.empty()) {
        std::cout << "No AV services found.\n";
        return;
    }
    for (const auto& h : hosts) {
        std::cout << format_ipv4(h.ip);
        if (!h.guess.empty()) std::cout << "  [" << h.guess << "]";
        std::cout << "\n";
        for (const auto& p : h.ports) {
            std::cout << "  " << std::right << std::setw(5) << p.port << "/tcp  "
                      << std::left << std::setw(28) << p.service << "  "
                      << (p.vendor.empty() ? "-" : p.vendor);
            if (!p.info.empty()) std::cout << "  | " << p.info;
            std::cout << "\n";
        }
        std::cout << "\n";
    }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

static const int      DEFAULT_WORKERS = 64;
static const int      MAX_WORKERS = 1024;
static const int      DEFAULT_TIMEOUT_MS = 500;
static const uint64_t MAX_HOSTS_UNFORCED = 65536;   // a /16

static void print_usage(std::ostream& os) {
    os << "Usage: av-net-scanner [options] <target>\n"
          "\n"
          "TCP connect scan for AV devices. UDP/mDNS-only devices (e.g. most Dante\n"
          "gear) are not visible to this tool.\n"
          "\n"
          "Target (exactly one; options may come before or after it):\n"
          "  a.b.c.d            single host\n"
          "  a.b.c.d-N          range over the last octet, e.g. 10.0.0.1-254\n"
          "  a.b.c.d-e.f.g.h    explicit address range\n"
          "  a.b.c.d/prefix     CIDR, prefix 0-32 (/31 and /32 include every address)\n"
          "\n"
          "Options:\n"
          "  --ports LIST       comma-separated TCP ports to probe instead of the\n"
          "                     built-in AV port table, e.g. --ports 23,1710,41795\n"
          "  --workers N        hosts scanned in parallel, 1-" << MAX_WORKERS << " (default " << DEFAULT_WORKERS << ")\n"
          "  --timeout MS       connect / banner timeout in ms, 10-60000 (default " << DEFAULT_TIMEOUT_MS << ")\n"
          "  --banner           read banners to identify Extron / Biamp Tesira /\n"
          "                     Crestron on ports 23, 41795 and unlisted --ports\n"
          "                     ports (default)\n"
          "  --no-banner        connect only, never read or send anything\n"
          "  --json             JSON array of hosts on stdout\n"
          "  --force            allow scans larger than " << MAX_HOSTS_UNFORCED << " addresses (a /16)\n"
          "  -h, --help         show this help\n"
          "\n"
          "Exit codes: 0 = something found, 1 = nothing found, 2 = usage error.\n";
}

static int usage_error(const std::string& msg) {
    std::cerr << "av-net-scanner: " << msg << "\n"
              << "Try 'av-net-scanner --help' for usage.\n";
    return 2;
}

struct Options {
    std::string           target;
    std::vector<uint16_t> ports;        // empty = built-in table
    bool                  custom_ports = false;
    int                   workers = DEFAULT_WORKERS;
    int                   timeout_ms = DEFAULT_TIMEOUT_MS;
    bool                  banner = true;
    bool                  json = false;
    bool                  force = false;
    bool                  help = false;
};

// Returns an empty string on success, otherwise the error message.
static std::string parse_args(int argc, char* argv[], Options& opt) {
    bool have_target = false;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg.empty()) return "empty argument";
        if (arg[0] != '-') {
            if (have_target) return "more than one target given ('" + opt.target + "' and '" + arg + "')";
            opt.target = arg;
            have_target = true;
            continue;
        }

        std::string name = arg, value;
        bool has_value = false;
        size_t eq = arg.find('=');
        if (eq != std::string::npos) {
            name = arg.substr(0, eq);
            value = arg.substr(eq + 1);
            has_value = true;
        }
        const bool takes_value = (name == "--ports" || name == "--workers" || name == "--timeout");
        if (takes_value && !has_value) {
            if (i + 1 >= argc) return "option " + name + " requires a value";
            value = argv[++i];
        } else if (!takes_value && has_value) {
            return "option " + name + " does not take a value";
        }

        uint64_t v = 0;
        std::string err;
        if (name == "--help" || name == "-h") opt.help = true;
        else if (name == "--json") opt.json = true;
        else if (name == "--force") opt.force = true;
        else if (name == "--banner") opt.banner = true;
        else if (name == "--no-banner") opt.banner = false;
        else if (name == "--ports") {
            if (!parse_port_list(value, opt.ports, err)) return err;
            opt.custom_ports = true;
        } else if (name == "--workers") {
            if (!parse_uint(value, MAX_WORKERS, v) || v < 1)
                return "invalid --workers value '" + value + "' (expected 1-" + std::to_string(MAX_WORKERS) + ")";
            opt.workers = static_cast<int>(v);
        } else if (name == "--timeout") {
            if (!parse_uint(value, 60000, v) || v < 10)
                return "invalid --timeout value '" + value + "' (expected 10-60000 ms)";
            opt.timeout_ms = static_cast<int>(v);
        } else {
            return "unknown option '" + name + "'";
        }
    }
    if (!opt.help && !have_target) return "no target given";
    return "";
}

// Upper bound on workers so that workers * sockets-per-worker fits in the
// process's file descriptor limit (POSIX only; tries to raise the soft limit).
static int fd_limited_workers(int workers, size_t sockets_per_worker) {
#ifdef _WIN32
    (void)sockets_per_worker;
    return workers;
#else
    struct rlimit rl{};
    if (getrlimit(RLIMIT_NOFILE, &rl) != 0) return workers;
    rlim_t want = static_cast<rlim_t>(workers) * sockets_per_worker + 64;
    if (rl.rlim_cur != RLIM_INFINITY && rl.rlim_cur < want) {
        struct rlimit raised = rl;
        raised.rlim_cur = (rl.rlim_max == RLIM_INFINITY || rl.rlim_max > want) ? want : rl.rlim_max;
        if (setrlimit(RLIMIT_NOFILE, &raised) == 0) rl = raised;
    }
    if (rl.rlim_cur == RLIM_INFINITY || rl.rlim_cur >= want) return workers;
    rlim_t usable = rl.rlim_cur > 64 ? rl.rlim_cur - 64 : 1;
    rlim_t fit = usable / sockets_per_worker;
    return static_cast<int>(std::max<rlim_t>(1, std::min<rlim_t>(fit, static_cast<rlim_t>(workers))));
#endif
}

static int run(const Options& opt) {
    Target target;
    std::string err;
    if (!parse_target(opt.target, target, err)) return usage_error(err);
    if (target.count == 0) return usage_error("target '" + opt.target + "' contains no addresses");
    if (target.count > MAX_HOSTS_UNFORCED && !opt.force)
        return usage_error("target '" + opt.target + "' covers " + std::to_string(target.count) +
                           " addresses; refusing more than " + std::to_string(MAX_HOSTS_UNFORCED) +
                           " (a /16) without --force");

    // Build the probe list.
    std::vector<ScanPort> ports;
    if (opt.custom_ports) {
        for (uint16_t p : opt.ports) {
            const PortDef* d = find_port_def(p);
            if (d) ports.push_back({p, d->service, d->vendor, d->unique, d->banner, d->banner});
            else   ports.push_back({p, "Unknown", "", "", true, false});   // listen only, never nudge
        }
    } else {
        for (const auto& d : AV_PORTS)
            ports.push_back({d.port, d.service, d.vendor, d.unique, d.banner, d.banner});
    }

    int workers = opt.workers;
    if (static_cast<uint64_t>(workers) > target.count) workers = static_cast<int>(target.count);
    int fd_workers = fd_limited_workers(workers, std::min(ports.size(), PORT_BATCH));
    if (fd_workers < workers) {
        std::cerr << "av-net-scanner: note: limiting workers to " << fd_workers
                  << " (open file limit)\n";
        workers = fd_workers;
    }

    const bool show_progress = !opt.json && stderr_is_tty();
    const auto started = Clock::now();

    std::atomic<uint64_t> next{0};
    std::atomic<uint64_t> done{0};
    std::mutex result_mutex;
    std::vector<HostResult> results;

    auto worker = [&]() {
        while (true) {
            uint64_t i = next.fetch_add(1, std::memory_order_relaxed);
            if (i >= target.count) break;
            HostResult h = scan_host(target.first + static_cast<uint32_t>(i), ports, opt.timeout_ms, opt.banner);
            if (!h.ports.empty()) {
                std::lock_guard<std::mutex> lock(result_mutex);
                results.push_back(std::move(h));
            }
            done.fetch_add(1, std::memory_order_relaxed);
        }
    };

    std::vector<std::thread> pool;
    pool.reserve(static_cast<size_t>(workers));
    for (int w = 0; w < workers; ++w) pool.emplace_back(worker);

    if (show_progress) {
        while (true) {
            uint64_t d = done.load(std::memory_order_relaxed);
            std::cerr << "\rScanning: " << d << "/" << target.count << " hosts" << std::flush;
            if (d >= target.count) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        std::cerr << "\r" << std::string(60, ' ') << "\r";
    }
    for (auto& t : pool) t.join();

    std::sort(results.begin(), results.end(),
              [](const HostResult& a, const HostResult& b) { return a.ip < b.ip; });

    if (opt.json) print_json(results);
    else          print_text(results);
    std::cout.flush();

    double secs = std::chrono::duration<double>(Clock::now() - started).count();
    std::cerr << "Scanned " << target.count << " host(s) x " << ports.size() << " port(s): "
              << results.size() << " host(s) responding, "
              << std::fixed << std::setprecision(2) << secs << " s elapsed\n";

    return results.empty() ? 1 : 0;
}

int main(int argc, char* argv[]) {
    Options opt;
    std::string err = parse_args(argc, argv, opt);
    if (opt.help) {
        print_usage(std::cout);
        return 0;
    }
    if (!err.empty()) return usage_error(err);

#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::cerr << "av-net-scanner: WSAStartup failed\n";
        return 2;
    }
#else
    std::signal(SIGPIPE, SIG_IGN);
#endif

    int rc = run(opt);

#ifdef _WIN32
    WSACleanup();
#endif
    return rc;
}
