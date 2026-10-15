"""Conformance check: the official MCP Python SDK, an independent implementation, can use this server.

Skipped unless the SDK is installed: pip install -e ".[interop]"
"""

import sys

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def wire(model) -> dict:
    return model.model_dump(by_alias=True, mode="json")


async def test_official_client_can_use_the_server(db_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "sqlite_mcp", str(db_path)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"list_tables", "describe_table", "run_query"}

            # Compare the wire form (camelCase), which is the same across SDK versions.
            result = wire(await session.call_tool("run_query", {"sql": "SELECT COUNT(*) FROM authors"}))
            assert result["isError"] is False
            assert result["structuredContent"]["rows"] == [[6]]

            bad = wire(await session.call_tool("run_query", {"sql": "DELETE FROM authors"}))
            assert bad["isError"] is True

            schema = wire(await session.read_resource("sqlite://schema"))
            assert "CREATE TABLE authors" in schema["contents"][0]["text"]

            prompt = wire(await session.get_prompt("ask_database", {"question": "Who sells best?"}))
            assert "Who sells best?" in prompt["messages"][0]["content"]["text"]
