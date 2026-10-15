"""The MCP layer, driven with raw JSON-RPC messages."""

from sqlite_mcp.jsonrpc import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR
from sqlite_mcp.protocol import RESOURCE_NOT_FOUND, SUPPORTED_PROTOCOL_VERSIONS


# -- lifecycle -------------------------------------------------------------------------


def test_initialize_handshake(client):
    reply = client.initialize()
    result = reply["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "sqlite-mcp-server"
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    assert "list_tables" in result["instructions"]
    assert client.server.initialized


def test_older_supported_version_is_echoed(client):
    assert client.initialize("2024-11-05")["result"]["protocolVersion"] == "2024-11-05"


def test_unknown_version_gets_servers_latest(client):
    result = client.initialize("1999-01-01")["result"]
    assert result["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_requests_before_initialize_are_refused(client):
    assert client.request("tools/list")["error"]["code"] == INVALID_REQUEST
    assert client.request("ping") == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_initialize_twice_is_an_error(ready):
    reply = ready.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert reply["error"]["code"] == INVALID_REQUEST


def test_initialize_requires_protocol_version(client):
    assert client.request("initialize", {})["error"]["code"] == INVALID_PARAMS


# -- JSON-RPC behaviour through the server ---------------------------------------------


def test_unknown_method(ready):
    assert ready.request("tools/explode")["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_never_get_a_reply(ready):
    ready.notify("notifications/cancelled", {"requestId": 1})
    ready.notify("some/unknown/notification")


def test_parse_error_has_null_id(ready):
    reply = ready.send("{oops")
    assert reply["id"] is None and reply["error"]["code"] == PARSE_ERROR


def test_batches_are_rejected(ready):
    reply = ready.send('[{"jsonrpc":"2.0","id":1,"method":"ping"}]')
    assert reply["error"]["code"] == INVALID_REQUEST


def test_handler_crash_becomes_internal_error_and_session_survives(ready):
    def boom():
        raise RuntimeError("bug")

    ready.server.tools["list_tables"].handler = boom
    assert ready.request("tools/call", {"name": "list_tables"})["error"]["code"] == -32603
    assert "result" in ready.request("ping")


# -- tools -----------------------------------------------------------------------------


def test_tools_list(ready):
    tools = {t["name"]: t for t in ready.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"list_tables", "describe_table", "run_query"}
    assert tools["run_query"]["inputSchema"]["required"] == ["sql"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())


def test_list_tables(ready):
    result = ready.call("list_tables")
    assert result["isError"] is False
    assert result["structuredContent"]["tables"] == [
        "authors", "book_sales", "books", "customers", "order_items", "orders"
    ]


def test_describe_table(ready):
    info = ready.call("describe_table", table="books")["structuredContent"]
    assert info["row_count"] == 13
    assert {"column": "author_id", "references_table": "authors", "references_column": "id"} in info["foreign_keys"]


def test_describe_unknown_table_is_a_tool_error(ready):
    result = ready.call("describe_table", table="nope")
    assert result["isError"] is True
    assert "Available: authors" in result["content"][0]["text"]


def test_run_query_returns_text_and_structured_content(ready):
    result = ready.call("run_query", sql="SELECT name FROM authors WHERE country = 'India'")
    assert result["structuredContent"]["rows"] == [["R. K. Narayan"]]
    assert '"R. K. Narayan"' in result["content"][0]["text"]


def test_writes_are_tool_errors_not_protocol_errors(ready):
    result = ready.call("run_query", sql="DROP TABLE books")
    assert result["isError"] is True
    assert "read-only" in result["content"][0]["text"]


def test_bad_arguments_are_reported_to_the_model(ready):
    assert "missing required argument 'sql'" in ready.call("run_query")["content"][0]["text"]
    assert "must be of type integer" in ready.call("run_query", sql="SELECT 1", limit="5")["content"][0]["text"]
    assert "must be <= 1000" in ready.call("run_query", sql="SELECT 1", limit=5000)["content"][0]["text"]
    assert "unexpected argument" in ready.call("list_tables", extra=1)["content"][0]["text"]


def test_unknown_tool_is_a_protocol_error(ready):
    reply = ready.request("tools/call", {"name": "rm_rf", "arguments": {}})
    assert reply["error"]["code"] == INVALID_PARAMS


# -- resources -------------------------------------------------------------------------


def test_resources_list_and_read_schema(ready):
    resources = ready.request("resources/list")["result"]["resources"]
    assert [r["uri"] for r in resources] == ["sqlite://schema"]
    contents = ready.request("resources/read", {"uri": "sqlite://schema"})["result"]["contents"]
    assert "CREATE TABLE books" in contents[0]["text"]
    assert "CREATE VIEW book_sales" in contents[0]["text"]


def test_resource_template(ready):
    templates = ready.request("resources/templates/list")["result"]["resourceTemplates"]
    assert templates[0]["uriTemplate"] == "sqlite://table/{table}"
    contents = ready.request("resources/read", {"uri": "sqlite://table/authors"})["result"]["contents"]
    assert contents[0]["mimeType"] == "application/json"
    assert '"row_count": 6' in contents[0]["text"]


def test_unknown_resource(ready):
    for uri in ("sqlite://nope", "sqlite://table/nope"):
        reply = ready.request("resources/read", {"uri": uri})
        assert reply["error"]["code"] == RESOURCE_NOT_FOUND


# -- prompts ---------------------------------------------------------------------------


def test_prompts(ready):
    prompts = ready.request("prompts/list")["result"]["prompts"]
    assert prompts[0]["name"] == "ask_database"
    assert prompts[0]["arguments"][0] == {
        "name": "question", "description": "What you want to know about the data.", "required": True
    }

    result = ready.request("prompts/get", {"name": "ask_database", "arguments": {"question": "Top city?"}})["result"]
    message = result["messages"][0]
    assert message["role"] == "user"
    assert "Top city?" in message["content"]["text"]


def test_prompt_argument_errors(ready):
    assert ready.request("prompts/get", {"name": "ask_database"})["error"]["code"] == INVALID_PARAMS
    assert ready.request("prompts/get", {"name": "nope"})["error"]["code"] == INVALID_PARAMS
