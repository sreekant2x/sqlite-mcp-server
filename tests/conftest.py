import json
from pathlib import Path

import pytest

from sqlite_mcp.app import build_server
from sqlite_mcp.database import Database
from sqlite_mcp.sample_data import build


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build(tmp_path / "bookstore.db")


@pytest.fixture
def db(db_path: Path):
    database = Database(db_path, timeout_seconds=1)
    yield database
    database.close()


class RawClient:
    """Drives the server with raw JSON-RPC text, the same bytes a real client would send."""

    def __init__(self, server):
        self.server = server
        self.next_id = 0

    def send(self, message) -> dict | None:
        raw = message if isinstance(message, str) else json.dumps(message)
        reply = self.server.handle_line(raw)
        return json.loads(reply) if reply is not None else None

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        reply = self.send(msg)
        assert reply["id"] == self.next_id
        return reply

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self.send(msg) is None

    def initialize(self, version: str = "2025-06-18") -> dict:
        reply = self.request(
            "initialize",
            {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
        )
        self.notify("notifications/initialized")
        return reply

    def call(self, tool: str, **arguments) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments})["result"]


@pytest.fixture
def client(db) -> RawClient:
    return RawClient(build_server(db))


@pytest.fixture
def ready(client) -> RawClient:
    client.initialize()
    return client
