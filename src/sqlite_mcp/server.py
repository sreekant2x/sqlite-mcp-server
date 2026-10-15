"""The MCP server, built on the official Python SDK (FastMCP).

The SDK handles everything between the model and this file: JSON-RPC 2.0 framing, the
stdio transport, the initialize handshake and capability negotiation, tools/list,
resources, prompts, argument-schema generation from type hints, and turning an exception
raised in a tool into a tool result with ``isError: true``.

What this file owns is the application: three tools, a schema resource plus a per-table
resource template, and one prompt, all backed by the read-only SQLite layer in
``database.py``. The server does no natural-language work — the client's model reads the
tool descriptions and schema, writes the SQL itself, and calls ``run_query``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .database import DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, Database, ToolError

INSTRUCTIONS = (
    "Read-only access to a SQLite database. Call list_tables, then describe_table on the "
    "tables you need, before writing SQL for run_query. Only SELECT statements are allowed."
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build_server(db: Database) -> FastMCP:
    mcp = FastMCP(name="sqlite-mcp-server", instructions=INSTRUCTIONS)

    # -- tools: things the model decides to call -----------------------------------------

    @mcp.tool(
        description="List the tables and views in the database.",
        annotations=_READ_ONLY,
    )
    def list_tables() -> dict:
        return {"tables": db.list_tables()}

    @mcp.tool(
        description="Show a table's columns, types, primary and foreign keys, and row count.",
        annotations=_READ_ONLY,
    )
    def describe_table(table: Annotated[str, Field(description="Exact table name from list_tables.")]) -> dict:
        try:
            return db.describe_table(table)
        except LookupError:
            raise ToolError(f"No table named {table!r}. Available: {', '.join(db.list_tables())}") from None

    @mcp.tool(
        description=(
            "Run one read-only SQLite SELECT statement (WITH ... SELECT is fine) and return the rows. "
            "Writes, PRAGMA, ATTACH and multiple statements are rejected. Results are capped at "
            "'limit' rows; 'truncated' is true when more rows exist."
        ),
        annotations=_READ_ONLY,
    )
    def run_query(
        sql: Annotated[str, Field(min_length=1, description="A single SELECT statement.")],
        limit: Annotated[int, Field(ge=1, le=MAX_ROW_LIMIT, description="Maximum rows to return.")] = DEFAULT_ROW_LIMIT,
    ) -> dict:
        return db.run_query(sql, limit)

    # -- resources: data the client application can attach as context ------------------

    @mcp.resource(
        "sqlite://schema",
        name="Database schema",
        description="CREATE statements for every table, view and index.",
        mime_type="text/plain",
    )
    def schema() -> str:
        return db.schema_sql()

    @mcp.resource(
        "sqlite://table/{table}",
        name="Table description",
        description="Columns, keys and row count for one table, as JSON.",
        mime_type="application/json",
    )
    def table_resource(table: str) -> str:
        return json.dumps(db.describe_table(table), indent=2)

    # -- prompts: templates the user picks, e.g. as a slash command ----------------------

    @mcp.prompt(description="Answer a question in plain language by exploring the schema and querying the data.")
    def ask_database(question: str) -> str:
        return (
            f"Answer this question using the SQLite database: {question}\n\n"
            "Work step by step: call list_tables, then describe_table for the relevant tables, "
            "then write one SELECT for run_query. If the result looks wrong, inspect the data "
            "and try again. Finish with the answer in plain language and the SQL you used."
        )

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only SQLite MCP server over stdio.")
    parser.add_argument("database", help="Path to the SQLite file to expose.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-query time limit in seconds.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    # stderr only: stdout belongs to the protocol. The SDK writes its messages to stdout.
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        db = Database(args.database, timeout_seconds=args.timeout)
    except FileNotFoundError as exc:
        parser.exit(2, f"error: {exc}\n")

    try:
        build_server(db).run(transport="stdio")
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
