from pathlib import Path

import pytest

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
