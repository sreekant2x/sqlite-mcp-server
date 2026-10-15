"""A minimal MCP client with no libraries, to watch the protocol on the wire.

    python scripts/raw_client.py bookstore.db

It launches the server as a subprocess, exactly like a desktop MCP client would, and prints
every line it sends (-->) and receives (<--).
"""

import json
import subprocess
import sys


def main(db_path: str) -> None:
    server = subprocess.Popen(
        [sys.executable, "-m", "sqlite_mcp", db_path, "--log-level", "WARNING"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
    )

    def send(message: dict) -> None:
        line = json.dumps(message)
        print(f"--> {line}")
        server.stdin.write(line + "\n")
        server.stdin.flush()

    def receive() -> dict:
        line = server.stdout.readline()
        print(f"<-- {line.rstrip()}\n")
        return json.loads(line)

    # 1. Handshake: agree on a protocol version and exchange capabilities.
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "raw-client", "version": "0.1"}}})
    receive()
    # 2. Tell the server we are ready. A notification: no id, and no reply comes back.
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    print()

    # 3. Discover what the server offers.
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    receive()

    # 4. Do what a model would do for "Which city spends the most?"
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "run_query", "arguments": {"sql": (
            "SELECT c.city, ROUND(SUM(oi.quantity * b.price_cents) / 100.0, 2) AS spent "
            "FROM customers c JOIN orders o ON o.customer_id = c.id "
            "JOIN order_items oi ON oi.order_id = o.id JOIN books b ON b.id = oi.book_id "
            "GROUP BY c.city ORDER BY spent DESC LIMIT 3")}}})
    receive()

    # 5. A write is refused as a tool result the model can read, not a protocol error.
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "run_query", "arguments": {"sql": "DELETE FROM orders"}}})
    receive()

    # 6. Closing stdin ends a stdio session.
    server.stdin.close()
    server.wait(timeout=5)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bookstore.db")
