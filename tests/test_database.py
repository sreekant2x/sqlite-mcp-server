"""The read-only guarantees. Every query here is something a model could plausibly send."""

import hashlib

import pytest

from sqlite_mcp.database import Database
from sqlite_mcp.protocol import ToolError


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM books",
        "UPDATE books SET price_cents = 0",
        "INSERT INTO authors (name, country) VALUES ('x', 'y')",
        "DROP TABLE books",
        "CREATE TABLE evil (x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA writable_schema = ON",
        "PRAGMA table_info(books)",
        "SELECT load_extension('evil')",
        "VACUUM",
        "WITH x AS (SELECT 1) DELETE FROM books",
    ],
)
def test_anything_but_select_is_refused(db, sql):
    with pytest.raises(ToolError):
        db.run_query(sql)


def test_stacked_statements_are_refused(db):
    with pytest.raises(ToolError):
        db.run_query("SELECT 1; DROP TABLE books")
    assert "books" in db.list_tables()


def test_database_file_is_unchanged(db, db_path):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    for sql in ("DELETE FROM books", "SELECT 1; DELETE FROM books", "UPDATE orders SET customer_id = 1"):
        with pytest.raises(ToolError):
            db.run_query(sql)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_runaway_query_is_stopped(db):
    with pytest.raises(ToolError, match="longer than"):
        db.run_query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c")


def test_row_limit_and_truncation(db):
    result = db.run_query("SELECT id FROM orders ORDER BY id", limit=10)
    assert result["row_count"] == 10 and result["truncated"] is True
    assert db.run_query("SELECT id FROM authors", limit=10)["truncated"] is False
    assert db.run_query("SELECT id FROM orders", limit=10**9)["row_count"] == 150  # capped at MAX_ROW_LIMIT


def test_server_side_introspection_still_works_after_a_refused_query(db):
    with pytest.raises(ToolError):
        db.run_query("PRAGMA table_info(books)")
    # The authorizer is only active during model-written queries.
    assert db.describe_table("books")["columns"][0]["name"] == "id"


def test_reads_joins_ctes_and_functions(db):
    result = db.run_query(
        "WITH spend AS ("
        "  SELECT c.city, SUM(oi.quantity * b.price_cents) AS cents"
        "  FROM customers c JOIN orders o ON o.customer_id = c.id"
        "  JOIN order_items oi ON oi.order_id = o.id JOIN books b ON b.id = oi.book_id"
        "  GROUP BY c.city)"
        " SELECT city, ROUND(cents / 100.0, 2) FROM spend ORDER BY 2 DESC LIMIT 1"
    )
    assert result["columns"] == ["city", "ROUND(cents / 100.0, 2)"]
    assert result["row_count"] == 1


def test_blobs_are_json_safe(db):
    assert db.run_query("SELECT x'cafe'")["rows"] == [[{"blob_hex": "cafe"}]]


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        Database(tmp_path / "missing.db")
