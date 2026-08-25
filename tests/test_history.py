"""Tests for session-scoped download history (issue #28)."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import db
import history
from app import app, history_processed_path
from settings import settings
from translation import jobs


async def _noop_translation(*_args, **_kwargs):
    """Stand-in for the background translation task: does nothing, fast."""
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated database, upload/processed folders and job store."""
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()

    monkeypatch.setattr(settings, "upload_folder", str(uploads))
    monkeypatch.setattr(settings, "processed_folder", str(processed))
    monkeypatch.setattr(settings, "file_retention_days", 7)

    conn = db.connect(str(tmp_path / "history.db"))
    monkeypatch.setattr(db, "connection", conn)

    jobs.clear()
    yield SimpleNamespace(conn=conn, uploads=uploads, processed=processed)
    jobs.clear()
    conn.close()


@pytest.fixture
def client(env):  # pylint: disable=unused-argument
    """Test client with its own cookie jar (i.e. its own browser session)."""
    return TestClient(app)


XLIFF = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
  <file source-language="en" target-language="da">
    <body>
      <trans-unit id="1"><source>Hello World</source></trans-unit>
    </body>
  </file>
</xliff>"""


def upload(test_client, filename="sample.xlf"):
    """Upload a file with the background translation stubbed out."""
    with patch("app.translate_xliff_with_progress", _noop_translation):
        response = test_client.post(
            "/upload", files={"file": (filename, XLIFF.encode(), "application/xml")}
        )
    assert response.status_code == 200
    return response.json()["job_id"]


def complete_job(env, job_id, original="sample.xlf", content=b"<xliff/>"):
    """Drive a job to the state translation.py leaves behind on success."""
    translated = f"translated_{original}"
    (env.processed / translated).write_bytes(content)
    jobs[job_id] = {
        "status": "completed",
        "completed": 1,
        "total": 1,
        "download_url": f"/download/{translated}",
        "error": None,
        "task": None,
    }
    return translated


def cookie_attributes(response):
    """Return the attributes of the session cookie as a lowercase set."""
    for raw in response.headers.get_list("set-cookie"):
        name = raw.split("=", 1)[0].strip()
        if name == history.SESSION_COOKIE_NAME:
            return {part.strip().lower() for part in raw.split(";")[1:]}
    return None


class TestSchema:
    """The migration appended to db.MIGRATIONS creates the history table."""

    def test_translations_table_exists(self, env):
        row = env.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='translations'"
        ).fetchone()
        assert row is not None

    def test_migrations_list_is_not_empty(self):
        assert len(db.MIGRATIONS) >= 1

    def test_job_id_is_unique(self, env):
        history.record_job(env.conn, "session-a", "job-1", "da")
        history.record_job(env.conn, "session-b", "job-1", "da")
        rows = env.conn.execute("SELECT session_id FROM translations").fetchall()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "session-a"


class TestSessionIdentity:
    """The session id is an opaque random value, validated before use."""

    def test_new_session_ids_are_unique_and_long(self):
        first, second = history.new_session_id(), history.new_session_id()
        assert first != second
        assert len(first) >= 32

    @pytest.mark.parametrize(
        "value", ["", "short", "has spaces here!!", "'; DROP TABLE translations;--", "x" * 200]
    )
    def test_rejects_malformed_session_ids(self, value):
        assert history.is_valid_session_id(value) is False

    def test_accepts_generated_session_id(self):
        assert history.is_valid_session_id(history.new_session_id()) is True


class TestCookieFlags:
    """HttpOnly, SameSite=Lax always; Secure only when the request is HTTPS."""

    def test_cookie_is_issued_on_first_request(self, client):
        response = client.get("/api/history")
        assert cookie_attributes(response) is not None

    def test_http_only_and_samesite_lax(self, client):
        attributes = cookie_attributes(client.get("/api/history"))
        assert "httponly" in attributes
        assert "samesite=lax" in attributes

    def test_not_secure_over_plain_http(self, client):
        assert "secure" not in cookie_attributes(client.get("/api/history"))

    def test_secure_over_https(self, env):  # pylint: disable=unused-argument
        secure_client = TestClient(app, base_url="https://testserver")
        assert "secure" in cookie_attributes(secure_client.get("/api/history"))

    def test_cookie_is_not_reissued_when_already_present(self, client):
        first = client.get("/api/history")
        second = client.get("/api/history")
        assert cookie_attributes(first) is not None
        assert cookie_attributes(second) is None


class TestRecordingOnCompletion:
    """A row is recorded for the uploading session and completed from the job store."""

    def test_upload_records_a_row(self, client, env):
        job_id = upload(client)
        row = env.conn.execute(
            "SELECT job_id, status FROM translations WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == history.STATUS_PROCESSING

    def test_history_reports_completion_with_download_url(self, client, env):
        job_id = upload(client)
        translated = complete_job(env, job_id)

        entries = client.get("/api/history").json()["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["job_id"] == job_id
        assert entry["status"] == history.VIEW_AVAILABLE
        assert entry["original_filename"] == "sample.xlf"
        assert entry["translated_filename"] == translated
        assert entry["download_url"] == f"/download/{translated}"
        assert entry["file_size"] == len(b"<xliff/>")
        assert entry["target_language"] == settings.default_target_language

    def test_progress_polling_persists_completion(self, client, env):
        job_id = upload(client)
        complete_job(env, job_id)

        client.get(f"/progress/{job_id}")  # the UI polls this every second
        row = env.conn.execute(
            "SELECT status, translated_filename FROM translations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["status"] == history.STATUS_COMPLETED
        assert row["translated_filename"] == "translated_sample.xlf"

    def test_failed_job_is_recorded_as_failed(self, client, env):
        job_id = upload(client)
        jobs[job_id] = {"status": "failed", "completed": 0, "total": 1,
                        "download_url": None, "error": "boom", "task": None}
        entry = client.get("/api/history").json()["entries"][0]
        assert entry["status"] == history.STATUS_FAILED
        assert entry["download_url"] is None

    def test_job_lost_to_a_restart_is_not_left_processing(self, client, env):
        job_id = upload(client)
        jobs.clear()  # the process restarted; the in-memory job store is gone
        entry = client.get("/api/history").json()["entries"][0]
        assert entry["status"] == history.STATUS_INTERRUPTED
        assert entry["download_url"] is None
        assert job_id == entry["job_id"]


class TestSessionIsolation:
    """A session sees its own rows and no others."""

    def test_second_session_cannot_see_first_sessions_rows(self, env):
        first = TestClient(app)
        second = TestClient(app)

        job_id = upload(first, "private.xlf")
        complete_job(env, job_id, "private.xlf")
        second.get("/api/history")  # second browser picks up its own cookie

        first_id = first.cookies.get(history.SESSION_COOKIE_NAME)
        second_id = second.cookies.get(history.SESSION_COOKIE_NAME)
        assert first_id and second_id and first_id != second_id
        assert len(first.get("/api/history").json()["entries"]) == 1
        assert second.get("/api/history").json()["entries"] == []

    def test_forged_session_cookie_sees_nothing(self, env):
        owner = TestClient(app)
        job_id = upload(owner, "private.xlf")
        complete_job(env, job_id, "private.xlf")

        attacker = TestClient(app)
        attacker.cookies.set(history.SESSION_COOKIE_NAME, history.new_session_id())
        assert attacker.get("/api/history").json()["entries"] == []

    def test_rows_are_filtered_in_sql_not_in_the_view(self, env):
        history.record_job(env.conn, "session-a", "job-a", "da")
        history.record_job(env.conn, "session-b", "job-b", "da")
        entries = history.list_history(
            env.conn, "session-a", retention_days=7, resolve_path=lambda _name: None
        )
        assert [entry["job_id"] for entry in entries] == ["job-a"]


class TestReDownload:
    """Re-download goes through the existing /download route."""

    def test_download_url_from_history_serves_the_file(self, client, env):
        job_id = upload(client)
        complete_job(env, job_id, content=b"<xliff>translated</xliff>")

        url = client.get("/api/history").json()["entries"][0]["download_url"]
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == b"<xliff>translated</xliff>"

    def test_resolved_path_is_inside_the_processed_folder(self, env):
        resolved = history_processed_path("translated_sample.xlf")
        assert resolved == os.path.join(str(env.processed), "translated_sample.xlf")

    @pytest.mark.parametrize(
        "filename", ["", "../../etc/passwd", "sub/dir/file.xlf", "..\\windows.xlf"]
    )
    def test_names_the_download_route_would_rewrite_are_rejected(self, env, filename):
        # pylint: disable=unused-argument
        assert history_processed_path(filename) is None

    def test_no_second_download_route_is_registered(self):
        download_routes = [
            route.path for route in app.routes if "download" in getattr(route, "path", "")
        ]
        assert download_routes == ["/download/{filename}"]


class TestUnavailableFiles:
    """Missing or expired files are reported, never linked."""

    def test_missing_file_is_unavailable(self, client, env):
        job_id = upload(client)
        translated = complete_job(env, job_id)
        client.get("/api/history")  # persist completion
        os.remove(env.processed / translated)

        entry = client.get("/api/history").json()["entries"][0]
        assert entry["status"] == history.VIEW_UNAVAILABLE
        assert entry["download_url"] is None

    def test_expired_row_is_reported_expired(self, env):
        history.record_job(env.conn, "session-a", "job-a", "da")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        env.conn.execute(
            "UPDATE translations SET created_at = ?, status = ?, translated_filename = ?"
            " WHERE job_id = 'job-a'",
            (old, history.STATUS_COMPLETED, "translated_old.xlf"),
        )
        env.conn.commit()

        entry = history.list_history(
            env.conn, "session-a", retention_days=7, resolve_path=lambda _name: None
        )[0]
        assert entry["status"] == history.VIEW_EXPIRED
        assert entry["download_url"] is None

    def test_expires_at_is_created_at_plus_retention(self, env):
        history.record_job(env.conn, "session-a", "job-a", "da")
        entry = history.list_history(
            env.conn, "session-a", retention_days=7, resolve_path=lambda _name: None
        )[0]
        created = datetime.fromisoformat(entry["created_at"])
        expires = datetime.fromisoformat(entry["expires_at"])
        assert expires - created == timedelta(days=7)


class TestReadOnlySurface:
    """The decision on #28: list and re-download only, no destructive endpoint."""

    def test_no_delete_history_endpoint(self, client):
        assert client.delete("/api/history").status_code in (404, 405)

    def test_no_post_history_endpoint(self, client):
        assert client.post("/api/history").status_code in (404, 405)

    def test_history_routes_are_get_only(self):
        for route in app.routes:
            if getattr(route, "path", "").startswith("/api/history"):
                assert set(route.methods) <= {"GET", "HEAD"}


class TestInterface:
    """The single-page interface carries the Recent Translations section."""

    def test_index_renders_history_section(self, client):
        page = client.get("/").text
        assert 'id="historySection"' in page
        assert "Recent Translations" in page
        assert 'src="/static/app.js"' in page

    def test_history_is_fetched_from_the_page_script(self, client):
        """
        The fetch moved out of the page and into /static/app.js.

        The Content-Security-Policy set by middleware.py has script-src 'self'
        with no 'unsafe-inline', so this section's script cannot live in the
        template; it would parse but never run.
        """
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert "/api/history" in script.text


class TestResilience:
    """History never breaks the request it rides along with."""

    def test_upload_still_succeeds_without_a_database(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_folder", str(tmp_path))
        monkeypatch.setattr(settings, "processed_folder", str(tmp_path))
        monkeypatch.setattr(db, "connection", None)
        jobs.clear()
        assert upload(TestClient(app)) is not None

    def test_history_endpoint_without_a_database_returns_empty(self, monkeypatch):
        monkeypatch.setattr(db, "connection", None)
        offline = TestClient(app)
        offline.cookies.set(history.SESSION_COOKIE_NAME, history.new_session_id())
        response = offline.get("/api/history")
        assert response.status_code == 200
        assert response.json()["entries"] == []
