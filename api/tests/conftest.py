"""Shared fixtures for API and UI tests."""
import json
import socket
import threading
import http.server
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).parent.parent / "static"


class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def static_server():
    """Serve api/static/ over HTTP for Playwright tests.

    Returns the base URL (e.g. http://localhost:54321).
    The session-scoped server stays up for the whole test run.
    """
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()

    server = http.server.HTTPServer(("localhost", port), _SilentHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://localhost:{port}"
    server.shutdown()
