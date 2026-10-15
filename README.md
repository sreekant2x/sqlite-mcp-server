# sqlite-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI agent
read-only access to a local SQLite database. Ask a connected agent "which city spends the most on
books?" and it explores the schema, writes the SQL, runs it here and answers.

The protocol is written by hand in plain Python, with no MCP SDK and no third-party
dependencies, so every layer between the model and the database is visible and tested.

```
 Agent host / IDE / Inspector  (the MCP client, which owns the LLM)
          |  stdin/stdout pipes
          v
 transport.py   Layer 2  newline-delimited messages over stdio
 jsonrpc.py     Layer 1  JSON-RPC 2.0 parsing, ids, error codes
 protocol.py    Layer 3  MCP lifecycle, capabilities, tools/resources/prompts
 app.py         Layer 4  the SQLite tools, resource and prompt
 database.py             read-only, sandboxed query execution
          |
          v
      bookstore.db
```

## Where the "natural language" actually happens

Not here. The server never sees an English question it has to interpret. The client's
model reads the tool descriptions, calls `list_tables` and `describe_table`, writes SQL
itself and sends it to `run_query`. The server's two jobs are to make that easy (clear
descriptions, schema on demand, errors the model can learn from) and to make it safe
(the model's SQL is untrusted input). That split is the whole point of MCP: the server
exposes capabilities, the model decides how to use them.

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/sreekant2x/sqlite-mcp-server
cd sqlite-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m sqlite_mcp.sample_data bookstore.db   # build the sample database
python scripts/raw_client.py bookstore.db       # watch a session on the wire
pytest                                          # run the tests
```

### Use it from a desktop MCP client

Add this to your client's MCP config file (for example `claude_desktop_config.json`), with absolute paths:

```json
{
  "mcpServers": {
    "bookstore": {
      "command": "/absolute/path/to/sqlite-mcp-server/.venv/bin/python",
      "args": ["-m", "sqlite_mcp", "/absolute/path/to/bookstore.db"]
    }
  }
}
```

Restart the client and ask "Which author has earned the most revenue, and from which book?"

### Inspect it with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m sqlite_mcp bookstore.db
```

## The layers, one by one

### Layer 1: JSON-RPC 2.0 ([`jsonrpc.py`](src/sqlite_mcp/jsonrpc.py))

MCP does not invent a wire format. Every message is a JSON-RPC 2.0 object, one of three kinds:

| Kind | Shape | Gets a reply? |
| --- | --- | --- |
| Request | `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}` | Yes, with the same `id` |
| Notification | `{"jsonrpc":"2.0","method":"notifications/initialized"}` | Never, not even an error |
| Response | `{"jsonrpc":"2.0","id":1,"result":{...}}` or `"error":{...}` | No |

The `id` is how replies are matched to requests, which is what lets a client have several
requests in flight. Errors use standard codes: `-32700` parse error, `-32600` invalid
request, `-32601` method not found, `-32602` invalid params, `-32603` internal error. When
a message is so broken the `id` can't be read, the error goes back with `"id": null`.

MCP narrows JSON-RPC in a few places, and this server enforces them: ids must be strings
or integers (never `null`), `params` must be an object, and batches (JSON arrays of
messages) were removed from MCP in protocol version 2025-06-18.

### Layer 2: the stdio transport ([`transport.py`](src/sqlite_mcp/transport.py))

The client starts the server as a child process. The client writes requests to the
server's stdin, one JSON message per line; the server writes replies to stdout, one per
line; the session ends when the client closes stdin.

Two mistakes kill stdio servers, and both are handled here:

- **Writing anything else to stdout.** A single debug `print()` corrupts the stream. All
  logging goes to stderr (`app.py` configures this), and `tests/test_stdio.py` asserts that
  every stdout line parses as JSON-RPC.
- **Not flushing.** stdout to a pipe is block-buffered, so an unflushed reply sits in a
  buffer and the client thinks the server hung. Every reply is flushed.

The transport knows nothing about MCP: it moves lines and calls a callback. MCP's other
transport, Streamable HTTP, would replace only this file.

### Layer 3: the MCP protocol ([`protocol.py`](src/sqlite_mcp/protocol.py))

**Lifecycle.** A session starts with a handshake:

```
--> initialize {protocolVersion: "2025-06-18", capabilities: {...}, clientInfo: {...}}
<-- {protocolVersion: "2025-06-18", capabilities: {tools, resources, prompts}, serverInfo, instructions}
--> notifications/initialized
```

*Version negotiation:* if the server supports the client's version it echoes it,
otherwise it answers with its own latest and the client decides whether to continue.
*Capability negotiation:* the server lists the method families it implements, so the
client knows not to call, say, `completion/complete`. Requests other than `initialize`
and `ping` before the handshake are refused.

**Three primitives,** distinguished by who decides to use them:

| Primitive | Controlled by | Methods | Here |
| --- | --- | --- | --- |
| Tools | the model | `tools/list`, `tools/call` | `list_tables`, `describe_table`, `run_query` |
| Resources | the application | `resources/list`, `resources/templates/list`, `resources/read` | `sqlite://schema`, `sqlite://table/{table}` |
| Prompts | the user | `prompts/list`, `prompts/get` | `ask_database` |

Each tool advertises a JSON Schema for its arguments; the server validates arguments
against it before calling the handler.

**Two kinds of errors,** and choosing the right one matters:

- *Protocol errors* (JSON-RPC `error`) mean the request itself was wrong: unknown method,
  unknown tool, malformed params. The client handles these; the model usually never sees them.
- *Tool errors* (a normal result with `"isError": true`) mean the tool ran and failed:
  bad SQL, a refused write, a timeout. The model sees the message and can fix its query
  and retry. Sending bad SQL back as a protocol error would hide the reason from the
  model, which is the one party that can fix it.

### Layer 4: the application ([`app.py`](src/sqlite_mcp/app.py), [`database.py`](src/sqlite_mcp/database.py))

The model writes the SQL, so every query is untrusted. Defence in depth:

1. The file is opened with SQLite's `mode=ro` URI flag, and `PRAGMA query_only` is on.
2. While a model-written query runs, an **authorizer callback** allows only `SELECT`,
   column reads and function calls (minus `load_extension`). SQLite asks the authorizer
   about every action in the *parsed* statement, so `INSERT`, `DROP`, `ATTACH`, `PRAGMA`
   and `WITH x AS (...) DELETE ...` are all refused before they run. A regex like
   "starts with SELECT" can't tell what a statement does; the parser can.
3. Python's `sqlite3` refuses to run more than one statement per call, so
   `SELECT 1; DROP TABLE books` fails outright.
4. A progress handler aborts any query running past a deadline (5 s by default), so an
   infinite recursive CTE can't hang the session.
5. Results are capped (100 rows by default, 1000 max) with a `truncated` flag, so the model
   knows to aggregate or filter instead of pulling the whole table into its context.

`tests/test_database.py` throws each of those attacks at the server and checks that the
database file's hash is unchanged afterwards.

## Tests

```bash
pytest                      # 58 tests, standard library only
pip install -e ".[interop]" # optional: the official MCP SDK
pytest                      # +1 test: the SDK's own client drives this server over stdio
```

- `test_jsonrpc.py`: framing and validation of raw messages.
- `test_protocol.py`: the MCP layer, driven by raw JSON-RPC strings.
- `test_database.py`: the read-only guarantees.
- `test_stdio.py`: the real process, real pipes.
- `test_sdk_interop.py`: an independent implementation (the official Python SDK client)
  completes a full session against this server. Hand-rolled protocol code is easy to get
  subtly wrong; this is the check that it isn't.

## What this deliberately leaves out

Being clear about the gaps is part of understanding the protocol:

- **Newer protocol versions.** The server speaks 2025-06-18 (and accepts 2025-03-26 and
  2024-11-05). Newer clients ask for a later version and accept the downgrade, which is
  how version negotiation is meant to work.
- **Streamable HTTP transport**, needed for remote servers and authentication.
- **Concurrency and cancellation.** Requests are handled one at a time, so
  `notifications/cancelled` is acknowledged but has nothing to cancel.
- **Pagination** of list results, `listChanged` notifications, resource subscriptions,
  logging, completions, and server-to-client requests such as sampling and elicitation.

## Project layout

```
src/sqlite_mcp/
  jsonrpc.py      Layer 1: JSON-RPC 2.0
  transport.py    Layer 2: stdio
  protocol.py     Layer 3: MCP lifecycle and primitives (generic, no SQLite)
  app.py          Layer 4: SQLite tools/resources/prompts, CLI entry point
  database.py     sandboxed read-only queries
  sample_data.py  builds bookstore.db
scripts/raw_client.py   dependency-free client that prints the wire traffic
tests/
```

## License

MIT
