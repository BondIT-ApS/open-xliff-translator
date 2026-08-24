# SQLite Persistence Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the application durable SQLite storage in a persistent volume, and split the 584-line `app.py` into focused modules so the vocabulary override and download history features have a foundation to build on.

**Architecture:** Python's stdlib `sqlite3` in WAL mode, with a single connection opened in the existing FastAPI lifespan context alongside the httpx client. Schema versioning is a `schema_version` table plus an ordered list of migration statements applied idempotently on startup. The backend splits into flat modules at the repository root (`settings.py`, `db.py`, `translation.py`, `app.py`) so `uvicorn app:app`, the Dockerfile `CMD`, and all deployment paths are unaffected.

**Tech Stack:** Python 3.13, FastAPI, stdlib `sqlite3`, pytest + pytest-asyncio, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-25-vocabulary-override-design.md`

**Issue:** #141

## Global Constraints

- **No new runtime dependencies.** Use stdlib `sqlite3`. Do not add `aiosqlite`, SQLAlchemy, or Alembic.
- **Flat modules at the repository root.** No package directory. `uvicorn app:app` and the Dockerfile `CMD` must not change.
- **`sqlite3` connections open with `check_same_thread=False`** and `PRAGMA journal_mode=WAL`.
- **Database path default:** `data/glossary.db`, overridable via the `DATABASE_PATH` environment variable.
- **Coverage gate stays enforced** at `--cov-fail-under=70` or higher. It must never be lowered to accommodate new modules.
- **XML parsing continues to use `defusedxml`**; `xml.etree.ElementTree` is for writing only.
- **Commit messages follow conventional commit format.** Never add AI attribution or `Generated with` signatures. Only use `Co-Authored-By` when explicitly requested.
- **Existing behaviour must not change.** This plan is a refactor plus new infrastructure; the 59 existing tests pass unchanged in behaviour (import paths may be updated).

---

### Task 1: Extract `settings.py`

Move the `Settings` class out of `app.py` so configuration has one home that every future module can import without pulling in the FastAPI app.

**Files:**
- Create: `settings.py`
- Modify: `app.py:1-65` (remove the Settings class and its instantiation, add the import)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing
- Produces: `settings.Settings` (a `pydantic_settings.BaseSettings` subclass) and `settings.settings`, the loaded singleton instance. Every later task imports `from settings import settings`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings.py`:

```python
"""Tests for application settings loading."""
from settings import Settings, settings


class TestSettings:
    """Settings load with correct defaults and can be overridden."""

    def test_defaults_are_loaded(self):
        s = Settings()
        assert s.log_level == "INFO"
        assert s.app_port == 5003
        assert s.upload_folder == "uploads"
        assert s.processed_folder == "processed"
        assert s.default_target_language == "da"

    def test_singleton_is_a_settings_instance(self):
        assert isinstance(settings, Settings)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TARGET_LANGUAGE", "de")
        s = Settings()
        assert s.default_target_language == "de"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_settings.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'settings'`

- [ ] **Step 3: Create `settings.py`**

Move the class verbatim from `app.py:22-54`. The full file:

```python
"""Application configuration, loaded from environment variables or a .env file."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    # Application settings
    log_level: str = "INFO"
    app_port: int = 5003

    # File management
    upload_folder: str = "uploads"
    processed_folder: str = "processed"

    # Translation settings
    libretranslate_url: str = "http://libretranslate:5000/translate"
    libretranslate_languages_url: str = "http://libretranslate:5000/languages"
    default_target_language: str = "da"

    # HTTP client settings
    http_timeout: float = 30.0
    http_connect_timeout: float = 10.0
    max_retries: int = 3
    max_connections: int = 10
    max_keepalive_connections: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Load settings once at import time
settings = Settings()
```

- [ ] **Step 4: Remove the class from `app.py` and import it**

Delete `app.py` lines 21-54 (the `# Settings configuration` comment through `settings = Settings()`). Also delete the now-unused import on line 17:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
```

Add in its place, with the other local imports:

```python
from settings import settings
```

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: PASS — all existing tests plus the 3 new ones. Coverage may dip below 70% because `--cov=app` no longer sees the moved lines; that is expected and is fixed in Task 6. If the gate fails, re-run with `--no-cov` to confirm the tests themselves pass, and proceed.

- [ ] **Step 6: Commit**

```bash
git add settings.py app.py tests/test_settings.py
git commit -m "refactor: extract Settings into settings.py"
```

---

### Task 2: Extract `translation.py`

Move all translation and placeholder logic out of `app.py`. This is the largest mechanical change; do it in one commit so the suite is never left half-migrated.

**Files:**
- Create: `translation.py`
- Modify: `app.py` (remove moved code, import the module)
- Modify: `tests/test_app.py:12-22` (import block) and the 11 `@patch('app.http_client')` decorators

**Interfaces:**
- Consumes: `settings.settings` from Task 1
- Produces, all in module `translation`:
  - `http_client: Optional[httpx.AsyncClient]` — module-level global, `None` until startup
  - `jobs: Dict[str, Dict[str, Any]]` — the in-memory job store
  - `async def startup_http_client() -> None`
  - `async def shutdown_http_client() -> None`
  - `def mask_placeholders(text: str) -> tuple[str, list[str]]`
  - `def restore_placeholders(text: str, originals: list[str]) -> str`
  - `def has_translatable_text(masked_text: str) -> bool`
  - `async def translate_text(text: str, target_lang: Optional[str] = None) -> str`
  - `async def translate_xliff_with_progress(job_id: str, input_file: str, output_file: str, target_lang: str) -> None`

**Critical detail:** `http_client` must stay a *module-level global accessed by name at call time*, not a value imported with `from translation import http_client`. Tests patch `translation.http_client`, and a from-import would bind `None` permanently at import time. In `app.py`, use `import translation` and reference `translation.http_client`.

- [ ] **Step 1: Create `translation.py`**

Move these blocks from `app.py` verbatim, in this order. **Locate each one by
name, not by line number** — PR #148 removed a function from `app.py` and every
line below it shifted, so any line number written here is already wrong:
- `http_client` global and `jobs` global
- `_PLACEHOLDER_PATTERN`
- `mask_placeholders`
- `_SENTINEL_PATTERN`
- `restore_placeholders`
- `has_translatable_text`
- `translate_text`
- `translate_xliff_with_progress`

The file header and the two new lifecycle functions:

```python
"""Translation pipeline: placeholder masking, LibreTranslate calls, XLIFF processing."""
import os
import re
import html
import logging
import asyncio
import xml.etree.ElementTree as ET  # nosec B405 - Only used for writing XML, not parsing
from typing import Any, Dict, Optional

import httpx
import defusedxml.ElementTree as DET
from fastapi import HTTPException

from settings import settings

logger = logging.getLogger(__name__)

# Global HTTP client, initialised by startup_http_client during app lifespan.
# Referenced by name (not from-imported) so tests can patch translation.http_client.
http_client: Optional[httpx.AsyncClient] = None

# In-memory job store: job_id -> job state dict
jobs: Dict[str, Dict[str, Any]] = {}


async def startup_http_client() -> None:
    """Create the shared httpx client. Called from the FastAPI lifespan."""
    global http_client  # pylint: disable=global-statement
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout, connect=settings.http_connect_timeout
        ),
        limits=httpx.Limits(
            max_keepalive_connections=settings.max_keepalive_connections,
            max_connections=settings.max_connections,
        ),
    )
    logger.info("HTTP client initialized")


async def shutdown_http_client() -> None:
    """Close the shared httpx client. Called from the FastAPI lifespan."""
    global http_client  # pylint: disable=global-statement
    if http_client:
        await http_client.aclose()
        http_client = None
    logger.info("HTTP client closed")
```

Then paste the nine moved blocks below, unchanged.

- [ ] **Step 2: Update `app.py`**

Delete the moved blocks and the now-unused imports (`re`, `html`, `asyncio` stays for `create_task`, `ET`, `DET`, `httpx` stays for the health check's exception handling — verify with pylint in Step 5).

Replace the lifespan body:

```python
@asynccontextmanager
async def lifespan(
    _app: FastAPI,
):  # pylint: disable=redefined-outer-name,unused-argument
    """Manage application lifespan for httpx client initialization and cleanup."""
    await translation.startup_http_client()
    logger.info("Application startup complete")
    yield
    await translation.shutdown_http_client()
    logger.info("Application shutdown complete")
```

Add near the other imports:

```python
import translation
from translation import jobs, translate_xliff_with_progress
```

In `health_check`, change `await http_client.get(...)` to:

```python
response = await translation.http_client.get(
    settings.libretranslate_languages_url, timeout=5.0
)
```

- [ ] **Step 3: Update the test import block**

In `tests/test_app.py`, replace lines 12-22 with:

```python
from app import (
    app,
    secure_filename,
    validate_path_in_directory,
)
from translation import (
    jobs,
    mask_placeholders,
    restore_placeholders,
    has_translatable_text,
    translate_text,
)
```

- [ ] **Step 4: Repoint the patch decorators**

Run:

```bash
sed -i '' "s/@patch('app\.http_client')/@patch('translation.http_client')/g" tests/test_app.py
grep -c "@patch('translation.http_client')" tests/test_app.py
```

Expected output: `11`

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest -v --no-cov
pylint app.py translation.py settings.py --rcfile=.pylintrc
```

Expected: all 62 tests PASS. Pylint reports no unused imports — if it flags any leftover import in `app.py`, remove it.

- [ ] **Step 6: Commit**

```bash
git add translation.py app.py tests/test_app.py
git commit -m "refactor: extract translation pipeline into translation.py"
```

---

### Task 3: Create `db.py` with schema versioning

The migration runner, built test-first. No application tables yet — this task delivers only the mechanism.

**Files:**
- Create: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `settings.settings` from Task 1
- Produces, all in module `db`:
  - `MIGRATIONS: list[str]` — ordered DDL statements; index + 1 is the version each one produces
  - `def connect(database_path: str) -> sqlite3.Connection` — opens with WAL, creates parent directories, applies pending migrations
  - `def current_version(conn: sqlite3.Connection) -> int`
  - `def apply_migrations(conn: sqlite3.Connection) -> int` — returns the version after applying; idempotent
  - `connection: Optional[sqlite3.Connection]` — module-level global, `None` until startup
  - `def startup_database() -> None` / `def shutdown_database() -> None`
  - `def get_connection() -> sqlite3.Connection` — raises `RuntimeError` if not started

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write `db.py`**

```python
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
MIGRATIONS: list[str] = []

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
```

Note `MIGRATIONS` is deliberately empty. The `elif version == 0` branch records version 0 so a fresh database with no migrations still has a row, which keeps `test_apply_migrations_is_idempotent` honest. Task 1 of the vocabulary plan appends the first entry.

- [ ] **Step 4: Add `database_path` to settings**

In `settings.py`, add under a new section after `# File management`:

```python
    # Persistence
    database_path: str = "data/glossary.db"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_db.py tests/test_settings.py -v --no-cov`
Expected: PASS, 13 tests

- [ ] **Step 6: Commit**

```bash
git add db.py settings.py tests/test_db.py
git commit -m "feat: add SQLite persistence layer with schema versioning"
```

---

### Task 4: Wire the database into the app lifespan and Docker

Storage that vanishes on rebuild is worse than no storage, so the volume and the lifespan wiring ship together.

**Files:**
- Modify: `app.py` (lifespan)
- Modify: `docker-compose.yml` (volume)
- Modify: `.env.template` (document `DATABASE_PATH`)
- Modify: `.gitignore` (ignore a local `data/` directory)
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `db.startup_database`, `db.shutdown_database` from Task 3
- Produces: a live `db.connection` for the process lifetime

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestLifespanWiring -v --no-cov`
Expected: FAIL — `assert db.connection is not None` fails, because nothing opens it.

- [ ] **Step 3: Wire the lifespan**

In `app.py`, add `import db` with the other local imports, and extend the lifespan:

```python
@asynccontextmanager
async def lifespan(
    _app: FastAPI,
):  # pylint: disable=redefined-outer-name,unused-argument
    """Manage application lifespan for HTTP client and database setup and cleanup."""
    await translation.startup_http_client()
    db.startup_database()
    logger.info("Application startup complete")
    yield
    db.shutdown_database()
    await translation.shutdown_http_client()
    logger.info("Application shutdown complete")
```

- [ ] **Step 4: Add the persistent volume**

In `docker-compose.yml`, add to the `open-xliff-translator` service `volumes:` list:

```yaml
      - openxliff_data:/app/data
```

And to the top-level `volumes:` block:

```yaml
  openxliff_data:
```

- [ ] **Step 5: Document the setting**

Append to `.env.template`, after the File Management section:

```
# ============================================
# Persistence
# ============================================

# SQLite database file. Must live on a mounted volume in Docker, or the
# vocabulary and history are lost on every image rebuild.
DATABASE_PATH=data/glossary.db
```

Add to `.gitignore`:

```
data/
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest -v --no-cov`
Expected: PASS, all tests

- [ ] **Step 7: Verify persistence end to end**

```bash
docker-compose up -d --build
docker exec open-xliff-translator sh -c "sqlite3 /app/data/glossary.db 'SELECT version FROM schema_version'" || \
  docker exec open-xliff-translator python -c "import sqlite3;print(sqlite3.connect('/app/data/glossary.db').execute('SELECT version FROM schema_version').fetchone())"
docker-compose down
docker-compose up -d --build
docker exec open-xliff-translator python -c "import sqlite3;print(sqlite3.connect('/app/data/glossary.db').execute('SELECT version FROM schema_version').fetchone())"
```

Expected: the same version reported both times, proving the volume survives a rebuild.

- [ ] **Step 8: Commit**

```bash
git add app.py docker-compose.yml .env.template .gitignore tests/test_db.py
git commit -m "feat: wire SQLite database into app lifespan with persistent volume"
```

---

### Task 5: Report database status from `/health`

**Files:**
- Modify: `app.py` (`HealthCheckResponse`, `health_check`)
- Test: `tests/test_app.py` (append to `TestHealthCheckEndpoint`)

**Interfaces:**
- Consumes: `db.get_connection` from Task 3
- Produces: `HealthCheckResponse.database: str` — `"ok"` or `"error"`

A database failure alone is `degraded`, not `unhealthy` — translation still works without it. Only LibreTranslate *and* the filesystem both failing produces a 503, matching the existing rule.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py` inside `TestHealthCheckEndpoint`:

```python
    @patch('translation.http_client')
    def test_health_reports_database_ok(self, mock_client, client, mock_httpx_languages):
        mock_client.get = AsyncMock(return_value=mock_httpx_languages)
        response = client.get("/health")
        assert response.json()["database"] == "ok"

    @patch('translation.http_client')
    def test_health_reports_database_error(self, mock_client, client, mock_httpx_languages):
        mock_client.get = AsyncMock(return_value=mock_httpx_languages)
        with patch('db.get_connection', side_effect=RuntimeError("no database")):
            response = client.get("/health")
        body = response.json()
        assert body["database"] == "error"
        assert body["status"] == "degraded"
        assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_app.py::TestHealthCheckEndpoint -v --no-cov`
Expected: FAIL with `KeyError: 'database'`

- [ ] **Step 3: Add the field and the check**

In `app.py`, extend the response model:

```python
class HealthCheckResponse(BaseModel):
    status: str
    libretranslate: str
    filesystem: str
    database: str
```

In `health_check`, initialise alongside the others:

```python
    database_status = "error"
```

Add this check after the filesystem check, before the overall-status determination:

```python
    # Check database
    try:
        db.get_connection().execute("SELECT 1").fetchone()
        database_status = "ok"
        logger.debug("Database health check passed")
    except Exception as e:  # pylint: disable=broad-except
        status = "degraded"
        logger.warning("Database health check failed: %s", e)
```

Extend both the log call and the return:

```python
    logger.info(
        "Health check: %s (LibreTranslate: %s, Filesystem: %s, Database: %s)",
        status,
        libretranslate_status,
        filesystem_status,
        database_status,
    )
    return HealthCheckResponse(
        status=status,
        libretranslate=libretranslate_status,
        filesystem=filesystem_status,
        database=database_status,
    )
```

Leave the existing `unhealthy` condition untouched — it still keys on LibreTranslate and the filesystem only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_app.py::TestHealthCheckEndpoint -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: report database status from health endpoint"
```

---

### Task 6: Widen CI coverage, lint, and documentation

The module split silently narrows what CI measures. This task closes that gap; without it the 70% gate becomes decorative.

**Files:**
- Modify: `pytest.ini`
- Modify: `.github/workflows/pr-quality-gate.yml:146`
- Modify: `.github/workflows/docker-publish.yml:89`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Determine what coverage currently measures**

```bash
pytest --cov-report=term-missing 2>&1 | tail -20
```

Note which files appear. If only `app.py` is listed, the `--cov=app` command-line argument is winning over `[coverage:run] source = .` and must be widened. If all modules appear, only the gate value needs review.

- [ ] **Step 2: Widen the coverage target**

`pytest.ini` is the single source of coverage scope — PR #151 removed the
explicit `--cov=app` both workflows used to pass, so widening it here is
sufficient and CI follows automatically. Replace the `--cov=app` line with
explicit module targets:

```
    --cov=app
    --cov=db
    --cov=settings
    --cov=translation
```

- [ ] **Step 3: Verify every module is measured**

```bash
pytest --cov-report=term-missing 2>&1 | tail -20
```

Expected: `app.py`, `db.py`, `settings.py`, and `translation.py` all appear with a coverage percentage.

- [ ] **Step 4: Raise the gate to the measured floor**

Read the reported TOTAL. Set `--cov-fail-under` in `pytest.ini` to that value rounded **down** to the nearest 5, and never below 70. Record the number in the commit message.

- [ ] **Step 5: Widen pylint in both workflows**

In `.github/workflows/pr-quality-gate.yml:146` and `.github/workflows/docker-publish.yml:89`, replace:

```yaml
          pylint app.py --rcfile=.pylintrc || true
```

with:

```yaml
          pylint app.py db.py settings.py translation.py --rcfile=.pylintrc || true
```

- [ ] **Step 6: Update `CLAUDE.md`**

Three edits:

1. Under **Key Characteristics**, replace `single-file backend (app.py)` with `modular backend (app.py, translation.py, db.py, settings.py)`.
2. Under **Architecture → Core Components**, replace the single `FastAPI Backend (app.py)` entry with a table of the four modules and their responsibilities. Remove the stale `app.py:NN` line-number citations rather than renumbering them — they will drift again.
3. Under **Testing**, correct `pytest test_app.py` to `pytest` (tests live in `tests/`, and `testpaths` is already set), and add a note that new modules must be added to the `--cov` list in `pytest.ini`.

Also correct the stale **CI/CD Pipeline** section: it lists `pylint.yml` and `codeql.yml`, neither of which exists. The real workflows are `docker-publish.yml`, `pr-quality-gate.yml`, and `weekly-security-report.yml`.

- [ ] **Step 7: Run the full suite with the gate enabled**

Run: `pytest`
Expected: PASS, gate satisfied.

- [ ] **Step 8: Commit**

```bash
git add pytest.ini .github/workflows/pr-quality-gate.yml .github/workflows/docker-publish.yml CLAUDE.md
git commit -m "chore: widen coverage and lint to new modules, refresh CLAUDE.md"
```

---

## Definition of Done

- [ ] `app.py` contains only app construction, routes, and lifespan
- [ ] `pytest` passes with the coverage gate enforced at 70% or higher
- [ ] `pylint app.py db.py settings.py translation.py --rcfile=.pylintrc` reports no errors
- [ ] `docker-compose up -d --build` twice in a row preserves the database
- [ ] `/health` reports `database: "ok"`
- [ ] `CLAUDE.md` describes the modular backend and the real workflow files
- [ ] No new entries in `requirements.txt`
