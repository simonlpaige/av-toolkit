// av-net-scanner.cpp — Fast AV device discovery on local subnets.
//
// Scans for common AV devices on a network by sending a minimal TCP
// SYN to known ports. Much faster than a full port scan because it
// targets only the ports that AV gear actually uses.
//
// Targets:
//   Port 23   — Extron, Biamp, Crestron (Telnet/SIS/console)
//   Port 80   — Web UI (Extron, Biamp, Q-SYS, Crestron)
//   Port 443  — HTTPS UI
//   Port 1702 — Q-SYS Core
//   Port 41795— Crestron CTP (control)
//   Port 41796— Crestron CIP (secure)
//   Port 50005— Dante control
//   Port 50006— Dante audio
//   Port 50007— Dante management
//   Port 5060 — VoIP/SIP
//   Port 8080 — Alternative web
//   Port 9999 — Crestron CWS
//
// Build:
//   g++ -std=c++17 -O2 av-net-scanner.cpp -o av-net-scanner -lws2_32
//
// Usage:
//   av-net-scanner 192.168.1.0/24
//   av-net-scanner 10.0.0.1-254 --workers 200
//   av-net-scanner 192.168.1.0/24 --json > devices.json
//
// Simon L. Paige — AV Toolkit
// License: MIT

#include <iostream>
#include <vector>
#include <string>
#include <thread>
#include <mutex>
#include <atomic>
#include <algorithm>
#include <sstream>
#include <iomanip>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #pragma comment(lib, "ws2_32.lib")
  using socklen_t = int;
#else
  #include <sys/socket.h>
  #include <netinet/in.h>
  #include <arpa/inet.h>
  #include <unistd.h>
  #include <fcntl.h>
  using SOCKET = int;
  #define INVALID_SOCKET (-1)
  #define SOCKET_ERROR (-1)
  #define closesocket close
  constexpr int WSAGetLastError() {
      return errno;
  }
#endif

struct AVDevice {
    std::string ip;
    int port;
    std::string service;
    std::string manufacturer;
};

// Known AV service ports
const std::vector<std::pair<int, std::pair<std::string, std::string>>> AV_PORTS = {
    {23,    {"Telnet/SIS",       "Extron/Biamp/Crestron"}},
    {80,    {"Web UI",           "Extron/Biamp/Q-SYS"}},
    {443,   {"HTTPS UI",         "All manufacturers"}},
    {1702,  {"Q-SYS Core",       "QSC"}},
    {41795, {"Crestron CTP",     "Crestron"}},
    {41796, {"Crestron Secure",  "Crestron"}},
    {50005, {"Dante Control",    "Audinate"}},
    {8080,  {"Alt Web UI",       "Various"}},
    {9999,  {"Crestron CWS",     "Crestron"}},
};

// AV device manufacturer guess based on port/service
std::string guess_manufacturer(int port) {
    switch (port) {
        case 1702: return "QSC";
        case 23:   return "Extron/Biamp";
        case 41795:
        case 41796:
        case 9999: return "Crestron";
        case 50005:
        case 50006:
        case 50007: return "Audinate (Dante)";
        default:   return "Unknown";
    }
}

bool tcp_ping(const std::string& ip, int port, int timeout_ms) {
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) return false;

    // Set non-blocking for fast timeout
#ifdef _WIN32
    u_long mode = 1;
    ioctlsocket(sock, FIONBIO, &mode);
#else
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);
#endif

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    inet_pton(AF_INET, ip.c_str(), &addr.sin_addr);

    connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

    fd_set fdset;
    FD_ZERO(&fdset);
    FD_SET(sock, &fdset);
    timeval tv{timeout_ms / 1000, (timeout_ms % 1000) * 1000};

    bool reachable = false;
    if (select(static_cast<int>(sock) + 1, nullptr, &fdset, nullptr, &tv) > 0) {
        int so_error = 0;
        socklen_t len = sizeof(so_error);
        getsockopt(sock, SOL_SOCKET, SO_ERROR,
#ifdef _WIN32
            reinterpret_cast<char*>(&so_error),
#else
            &so_error,
#endif
            &len);
        reachable = (so_error == 0);
    }

    closesocket(sock);
    return reachable;
}

std::vector<std::string> parse_range(const std::string& target) {
    std::vector<std::string> ips;

    // CIDR notation
    auto slash = target.find('/');
    if (slash != std::string::npos) {
        std::string base_ip = target.substr(0, slash);
        int prefix = std::stoi(target.substr(slash + 1));

        // Parse base IP
        unsigned long base = 0;
        inet_pton(AF_INET, base_ip.c_str(), &base);
        base = ntohl(base);

        unsigned long mask = (prefix == 0) ? 0 : (~0UL << (32 - prefix));
        unsigned long network = base & mask;
        unsigned long broadcast = network | ~mask;

        for (unsigned long i = network + 1; i < broadcast; ++i) {
            struct in_addr addr;
            addr.s_addr = htonl(i);
            char buf[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &addr, buf, sizeof(buf));
            ips.push_back(buf);
        }
    }
    // Range notation: 192.168.1.1-254
    else if (target.find('-') != std::string::npos) {
        auto dash = target.find_last_of('-');
        std::string ip_start = target.substr(0, dash);
        int start = std::stoi(ip_start.substr(ip_start.find_last_of('.') + 1));
        int end = std::stoi(target.substr(dash + 1));

        std::string prefix = ip_start.substr(0, ip_start.find_last_of('.') + 1);
        for (int i = start; i <= end; ++i) {
            ips.push_back(prefix + std::to_string(i));
        }
    }
    // Single IP
    else {
        ips.push_back(target);
    }

    return ips;
}

int main(int argc, char* argv[]) {
#ifdef _WIN32
    WSADATA wsaData;
    WSAStartup(MAKEWORD(2, 2), &wsaData);
#endif

    if (argc < 2) {
        std::cerr << "Usage: av-net-scanner <target>\n";
        std::cerr << "  target: IP, range (x.x.x.x-y), or CIDR (x.x.x.x/24)\n";
        std::cerr << "  --json: output as JSON\n";
        std::cerr << "  --workers N: concurrent scan threads (default 100)\n";
        std::cerr << "  --timeout N: TCP timeout ms (default 500)\n";
#ifdef _WIN32
        WSACleanup();
#endif
        return 1;
    }

    std::string target = argv[1];
    bool json_output = false;
    int workers = 100;
    int timeout_ms = 500;

    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--json") json_output = true;
        else if (arg == "--workers" && i + 1 < argc) workers = std::stoi(argv[++i]);
        else if (arg == "--timeout" && i + 1 < argc) timeout_ms = std::stoi(argv[++i]);
    }

    auto ips = parse_range(target);

    if (!json_output) {
        std::cerr << "Scanning " << ips.size() << " hosts × "
                  << AV_PORTS.size() << " ports...\n";
    }

    std::mutex result_mutex;
    std::vector<AVDevice> results;
    std::atomic<size_t> progress{0};
    std::atomic<size_t> total_checks{0};

    auto scan_host = [&](const std::string& ip) {
        for (const auto& [port, info] : AV_PORTS) {
            if (tcp_ping(ip, port, timeout_ms)) {
                AVDevice dev{ip, port, info.first, info.second};
                std::lock_guard<std::mutex> lock(result_mutex);
                results.push_back(dev);
            }
        }
        total_checks += AV_PORTS.size();
        ++progress;
    };

    // Thread pool
    std::vector<std::thread> pool;
    size_t idx = 0;
    for (int w = 0; w < workers && idx < ips.size(); ++w) {
        pool.emplace_back([&, idx]() {
            for (size_t i = idx; i < ips.size(); i += workers) {
                scan_host(ips[i]);
            }
        });
        ++idx;
    }

    for (auto& t : pool) t.join();

    // Output
    if (json_output) {
        std::cout << "[\n";
        for (size_t i = 0; i < results.size(); ++i) {
            const auto& d = results[i];
            std::cout << "  {\"ip\":\"" << d.ip
                      << "\",\"port\":" << d.port
                      << ",\"service\":\"" << d.service
                      << "\",\"manufacturer\":\"" << d.manufacturer << "\"}";
            if (i < results.size() - 1) std::cout << ",";
            std::cout << "\n";
        }
        std::cout << "]\n";
    } else {
        std::cout << "\nFound " << results.size() << " AV service(s):\n\n";
        std::cout << std::left
                  << std::setw(16) << "IP"
                  << std::setw(8) << "Port"
                  << std::setw(18) << "Service"
                  << "Manufacturer\n";
        std::cout << std::string(60, '-') << "\n";

        std::sort(results.begin(), results.end(), [](const AVDevice& a, const AVDevice& b) {
            if (a.ip != b.ip) return a.ip < b.ip;
            return a.port < b.port;
        });

        for (const auto& d : results) {
            std::cout << std::left
                      << std::setw(16) << d.ip
                      << std::setw(8) << d.port
                      << std::setw(18) << d.service
                      << d.manufacturer << "\n";
        }
    }

#ifdef _WIN32
    WSACleanup();
#endif
    return 0;
}
