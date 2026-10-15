"""Layer 4: the application. Wires a SQLite database into the generic MCP server.

Note what is *not* here: no natural-language handling. The client's model reads the
tool descriptions and schema, writes SQL itself and calls ``run_query``. The server's
job is to make that easy (good descriptions, schema on demand) and safe (read-only).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, transport
from .database import DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, Database
from .protocol import Prompt, PromptArgument, Resource, ResourceTemplate, Server, Tool, ToolError

INSTRUCTIONS = (
    "Read-only access to a SQLite database. Call list_tables, then describe_table on the "
    "tables you need, before writing SQL for run_query. Only SELECT statements are allowed."
)


def build_server(db: Database) -> Server:
    server = Server(name="sqlite-mcp-server", version=__version__, instructions=INSTRUCTIONS)

    # -- tools: things the model decides to call -----------------------------------------

    def list_tables() -> dict:
        return {"tables": db.list_tables()}

    def describe_table(table: str) -> dict:
        try:
            return db.describe_table(table)
        except LookupError:
            raise ToolError(f"No table named {table!r}. Available: {', '.join(db.list_tables())}") from None

    def run_query(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> dict:
        return db.run_query(sql, limit)

    server.add_tool(
        Tool(
            name="list_tables",
            description="List the tables and views in the database.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_tables,
        )
    )
    server.add_tool(
        Tool(
            name="describe_table",
            description="Show a table's columns, types, primary and foreign keys, and row count.",
            input_schema={
                "type": "object",
                "properties": {"table": {"type": "string", "description": "Exact table name from list_tables."}},
                "required": ["table"],
                "additionalProperties": False,
            },
            handler=describe_table,
        )
    )
    server.add_tool(
        Tool(
            name="run_query",
            description=(
                "Run one read-only SQLite SELECT statement (WITH ... SELECT is fine) and return the rows. "
                "Writes, PRAGMA, ATTACH and multiple statements are rejected. Results are capped at "
                "'limit' rows; 'truncated' is true when more rows exist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "description": "A single SELECT statement."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ROW_LIMIT,
                        "default": DEFAULT_ROW_LIMIT,
                        "description": "Maximum rows to return.",
                    },
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
            handler=run_query,
        )
    )

    # -- resources: data the client application can attach as context ------------------

    server.add_resource(
        Resource(
            uri="sqlite://schema",
            name="Database schema",
            description="CREATE statements for every table, view and index.",
            mime_type="text/plain",
            reader=db.schema_sql,
        )
    )
    server.add_resource_template(
        ResourceTemplate(
            uri_template="sqlite://table/{table}",
            name="Table description",
            description="Columns, keys and row count for one table, as JSON.",
            mime_type="application/json",
            reader=lambda table: json.dumps(db.describe_table(table), indent=2),
        )
    )

    # -- prompts: templates the user picks, e.g. as a slash command ----------------------

    def ask_database(question: str) -> str:
        return (
            f"Answer this question using the SQLite database: {question}\n\n"
            "Work step by step: call list_tables, then describe_table for the relevant tables, "
            "then write one SELECT for run_query. If the result looks wrong, inspect the data "
            "and try again. Finish with the answer in plain language and the SQL you used."
        )

    server.add_prompt(
        Prompt(
            name="ask_database",
            description="Answer a question in plain language by exploring the schema and querying the data.",
            arguments=[PromptArgument("question", "What you want to know about the data.", required=True)],
            render=ask_database,
        )
    )
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only SQLite MCP server over stdio.")
    parser.add_argument("database", help="Path to the SQLite file to expose.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-query time limit in seconds.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    # stderr only: stdout belongs to the protocol.
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        db = Database(args.database, timeout_seconds=args.timeout)
    except FileNotFoundError as exc:
        parser.exit(2, f"error: {exc}\n")

    transport.configure_stdio()
    try:
        transport.serve(build_server(db).handle_line)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
