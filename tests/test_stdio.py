"""End to end: launch the real process and speak raw JSON lines over its pipes."""

import json
import subprocess
import sys


def test_full_session_over_stdio(db_path):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "run_query", "arguments": {"sql": "SELECT COUNT(*) AS n FROM orders"}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in lines) + "\n\nnot json\n"
    proc = subprocess.run(
        [sys.executable, "-m", "sqlite_mcp", str(db_path)],
        input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    # Every stdout line must be a JSON-RPC message; logs belong on stderr.
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, None]  # the notification got no reply
    assert replies[2]["result"]["structuredContent"]["rows"] == [[150]]
    assert replies[3]["error"]["code"] == -32700
    assert "initialize from" in proc.stderr


def test_missing_database_exits_with_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "sqlite_mcp", str(tmp_path / "missing.db")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "not found" in proc.stderr
