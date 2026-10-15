"""End-to-end tests driving the server with the official MCP SDK client over stdio.

These launch the real `python -m sqlite_mcp <db>` process and speak to it exactly as a
real MCP client (Claude Desktop, an IDE) would, so they exercise the SDK's JSON-RPC
framing, stdio transport, handshake and dispatch along with our tools, resources and
prompts. Each test runs one full session via asyncio.run, so no async pytest plugin is
needed.
"""

import asyncio
import hashlib
import json
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def run_session(db_path, coro):
    params = StdioServerParameters(command=sys.executable, args=["-m", "sqlite_mcp", str(db_path)])

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await coro(session)

    return asyncio.run(main())


def wire(model) -> dict:
    """The JSON wire form (camelCase), stable across SDK versions."""
    return model.model_dump(by_alias=True, mode="json")


def payload(result: dict) -> dict:
    """A tool's JSON return value, read from its text content (what the model sees)."""
    return json.loads(result["content"][0]["text"])


def test_initialize_and_list_tools(db_path):
    async def go(session):
        tools = await session.list_tools()
        return {t.name for t in tools.tools}

    assert run_session(db_path, go) == {"list_tables", "describe_table", "run_query"}


def test_list_tables_and_describe(db_path):
    async def go(session):
        tables = wire(await session.call_tool("list_tables"))
        described = wire(await session.call_tool("describe_table", {"table": "books"}))
        return tables, described

    tables, described = run_session(db_path, go)
    assert tables["isError"] is False
    assert "books" in payload(tables)["tables"]
    assert payload(described)["row_count"] == 13


def test_run_query_returns_rows(db_path):
    async def go(session):
        return wire(await session.call_tool("run_query", {"sql": "SELECT name FROM authors WHERE country = 'India'"}))

    result = run_session(db_path, go)
    assert result["isError"] is False
    assert payload(result)["rows"] == [["R. K. Narayan"]]


@pytest.mark.parametrize("sql", ["DROP TABLE books", "DELETE FROM orders", "ATTACH DATABASE ':memory:' AS x", "PRAGMA table_info(books)"])
def test_writes_are_tool_errors_and_leave_db_unchanged(db_path, sql):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    async def go(session):
        return wire(await session.call_tool("run_query", {"sql": sql}))

    result = run_session(db_path, go)
    assert result["isError"] is True
    assert "read-only" in result["content"][0]["text"] or "not authorized" in result["content"][0]["text"]
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_bad_arguments_are_rejected_by_the_schema(db_path):
    async def go(session):
        return wire(await session.call_tool("run_query", {"sql": "SELECT 1", "limit": 99999}))

    # The SDK validates against the generated schema (limit <= 1000) before our handler runs.
    result = run_session(db_path, go)
    assert result["isError"] is True


def test_resources(db_path):
    async def go(session):
        listed = await session.list_resources()
        schema = wire(await session.read_resource("sqlite://schema"))
        table = wire(await session.read_resource("sqlite://table/authors"))
        return [str(r.uri) for r in listed.resources], schema, table

    uris, schema, table = run_session(db_path, go)
    assert "sqlite://schema" in uris
    assert "CREATE TABLE books" in schema["contents"][0]["text"]
    assert '"row_count": 6' in table["contents"][0]["text"]


def test_prompt(db_path):
    async def go(session):
        prompts = await session.list_prompts()
        got = wire(await session.get_prompt("ask_database", {"question": "Top city?"}))
        return {p.name for p in prompts.prompts}, got

    names, got = run_session(db_path, go)
    assert "ask_database" in names
    assert "Top city?" in got["messages"][0]["content"]["text"]
