"""Tests for the SQLite persistence layer."""
import os
import sqlite3

import pytest

import db


@pytest.fixture
def db_path(tmp_path):
    """A database path inside a temporary directory."""
    return str(tmp_path / "nested" / "test.db")


class TestConnect:
    """connect() creates the file, enables WAL, and migrates."""

    def test_creates_parent_directories(self, db_path):
        conn = db.connect(db_path)
        conn.close()
        assert os.path.exists(db_path)

    def test_enables_wal_mode(self, db_path):
        conn = db.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"

    def test_rows_are_accessible_by_name(self, db_path):
        conn = db.connect(db_path)
        row = conn.execute("SELECT 1 AS answer").fetchone()
        conn.close()
        assert row["answer"] == 1


class TestMigrations:
    """Migrations apply in order, exactly once."""

    def test_fresh_database_is_at_latest_version(self, db_path):
        conn = db.connect(db_path)
        assert db.current_version(conn) == len(db.MIGRATIONS)
        conn.close()

    def test_schema_version_table_exists(self, db_path):
        conn = db.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_apply_migrations_is_idempotent(self, db_path):
        conn = db.connect(db_path)
        first = db.apply_migrations(conn)
        second = db.apply_migrations(conn)
        conn.close()
        assert first == second == len(db.MIGRATIONS)

    def test_reopening_does_not_reapply(self, db_path):
        conn = db.connect(db_path)
        conn.close()
        conn = db.connect(db_path)
        assert db.current_version(conn) == len(db.MIGRATIONS)
        conn.close()

    def test_data_survives_reopen(self, db_path):
        conn = db.connect(db_path)
        conn.execute("CREATE TABLE probe (v TEXT)")
        conn.execute("INSERT INTO probe VALUES ('kept')")
        conn.commit()
        conn.close()

        conn = db.connect(db_path)
        row = conn.execute("SELECT v FROM probe").fetchone()
        conn.close()
        assert row["v"] == "kept"


class TestLifecycle:
    """startup/shutdown manage the module-level connection."""

    def test_get_connection_before_startup_raises(self):
        db.connection = None
        with pytest.raises(RuntimeError):
            db.get_connection()

    def test_startup_then_get_connection(self, db_path, monkeypatch):
        monkeypatch.setattr(db.settings, "database_path", db_path)
        db.startup_database()
        assert isinstance(db.get_connection(), sqlite3.Connection)
        db.shutdown_database()
        assert db.connection is None


class TestLifespanWiring:
    """The FastAPI lifespan opens and closes the database."""

    def test_lifespan_opens_and_closes_database(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from app import app as fastapi_app

        monkeypatch.setattr(
            db.settings, "database_path", str(tmp_path / "lifespan.db")
        )
        with TestClient(fastapi_app):
            assert db.connection is not None
        assert db.connection is None
