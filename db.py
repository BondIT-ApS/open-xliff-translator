"""SQLite persistence: connection management, schema versioning, migrations."""
import os
import logging
import sqlite3
from typing import Optional

from settings import settings

logger = logging.getLogger(__name__)

# Ordered schema migrations. The version a migration produces is its index + 1.
# NEVER edit or reorder an existing entry — append only. Editing one silently
# skips it on databases that already recorded a higher version.
MIGRATIONS: list[str] = [
    # 1 — download history (issue #28). One row per translation job, owned by
    # an anonymous browser session. Expiry is derived from file_retention_days
    # at read time rather than stored, and paths are derived from the
    # configured processed folder, so neither goes stale in the database.
    """
    CREATE TABLE IF NOT EXISTS translations (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          TEXT    NOT NULL,
        job_id              TEXT    NOT NULL UNIQUE,
        original_filename   TEXT,
        translated_filename TEXT,
        created_at          TEXT    NOT NULL,
        completed_at        TEXT,
        file_size           INTEGER,
        source_language     TEXT    NOT NULL DEFAULT 'auto',
        target_language     TEXT    NOT NULL,
        status              TEXT    NOT NULL DEFAULT 'processing'
    );
    CREATE INDEX IF NOT EXISTS idx_translations_session
        ON translations (session_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_translations_status
        ON translations (status);
    """,
]

# Module-level connection, initialised by startup_database during app lifespan.
connection: Optional[sqlite3.Connection] = None


def current_version(conn: sqlite3.Connection) -> int:
    """Return the schema version recorded in the database, or 0 if unversioned."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row["version"] if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply every migration above the recorded version. Idempotent."""
    version = current_version(conn)
    for index in range(version, len(MIGRATIONS)):
        logger.info("Applying schema migration %d", index + 1)
        conn.executescript(MIGRATIONS[index])
    new_version = len(MIGRATIONS)
    if new_version != version:
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (new_version,))
    elif version == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
    conn.commit()
    return new_version


def connect(database_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the database, enable WAL, and migrate to latest."""
    parent = os.path.dirname(os.path.abspath(database_path))
    os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    return conn


def startup_database() -> None:
    """Open the shared connection. Called from the FastAPI lifespan."""
    global connection  # pylint: disable=global-statement
    connection = connect(settings.database_path)
    logger.info("Database ready at %s", settings.database_path)


def shutdown_database() -> None:
    """Close the shared connection. Called from the FastAPI lifespan."""
    global connection  # pylint: disable=global-statement
    if connection:
        connection.close()
        connection = None
    logger.info("Database connection closed")


def get_connection() -> sqlite3.Connection:
    """Return the shared connection, or raise if the app has not started."""
    if connection is None:
        raise RuntimeError("Database not initialised — startup_database() was not called")
    return connection
