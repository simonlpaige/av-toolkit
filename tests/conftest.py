"""Test helpers: load the hyphen-named scripts as modules, and fake devices on localhost."""
import importlib.util
import socket
import socketserver
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EXAMPLES = ROOT / "examples"


def load_script(relpath: str):
    path = ROOT / relpath
    name = "script_" + relpath.replace("/", "_").replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def serve():
    """serve(handler_fn) -> port. handler_fn(sock) runs once per connection."""
    servers = []

    def start(handler_fn):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    handler_fn(self.request)
                except OSError:
                    pass
        server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
