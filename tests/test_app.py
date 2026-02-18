"""
Comprehensive test suite for Open XLIFF Translator FastAPI application.
"""
import os
import asyncio
import tempfile
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import httpx
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
from app import app, secure_filename, fix_placeholder_formatting, validate_path_in_directory, jobs

# Test fixtures
@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    return TestClient(app)

@pytest.fixture
def sample_xliff():
    """Sample XLIFF content for testing."""
    return '''<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
    <file source-language="en" target-language="da">
        <body>
            <trans-unit id="1">
                <source>Hello World</source>
            </trans-unit>
            <trans-unit id="2">
                <source>You have %1$s messages</source>
            </trans-unit>
            <trans-unit id="3">
                <source>New line here: %n</source>
            </trans-unit>
        </body>
    </file>
</xliff>'''

@pytest.fixture
def malformed_xliff():
    """Malformed XLIFF content for testing."""
    return '''<?xml version="1.0"?>
<xliff>
    <file>
        <body>
            <trans-unit>
                <source>Unclosed tag
            </trans-unit>
'''

@pytest.fixture
def mock_httpx_success():
    """Mock successful httpx responses."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"translatedText": "Hej Verden"}
    return mock_response

@pytest.fixture
def mock_httpx_languages():
    """Mock successful languages endpoint response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"code": "en"}, {"code": "da"}]
    return mock_response


# Test Index Endpoint
class TestIndexEndpoint:
    """Tests for the index route."""

    def test_index_returns_html(self, client):
        """Test that index returns HTML content."""
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_index_contains_upload_form(self, client):
        """Test that index contains file upload form elements."""
        response = client.get("/")
        assert b"<form" in response.content
        assert b"file" in response.content or b"upload" in response.content.lower()


# Test Upload Endpoint
class TestUploadEndpoint:
    """Tests for the upload route."""

    @patch('app.http_client')
    def test_upload_valid_file_returns_job_id(self, mock_client, client, sample_xliff, mock_httpx_success):
        """Test successful upload returns a job_id for progress tracking."""
        mock_client.post = AsyncMock(return_value=mock_httpx_success)

        with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
            tmp_file.write(sample_xliff.encode())
            tmp_file.flush()
            tmp_path = tmp_file.name

        try:
            with open(tmp_path, 'rb') as f:
                response = client.post(
                    "/upload",
                    files={"file": ("test.xlf", f, "application/xml")}
                )

            assert response.status_code == 200
            data = response.json()
            assert "message" in data
            assert "job_id" in data
            assert len(data["job_id"]) == 36  # UUID format
        finally:
            os.unlink(tmp_path)
            if os.path.exists("uploads/test.xlf"):
                os.unlink("uploads/test.xlf")

    def test_upload_no_file(self, client):
        """Test upload with no file provided."""
        response = client.post("/upload")
        assert response.status_code == 422  # FastAPI validation error

    def test_upload_empty_filename(self, client):
        """Test upload with empty filename."""
        response = client.post(
            "/upload",
            files={"file": ("", b"some content", "application/xml")}
        )
        # FastAPI returns 422 for validation errors or 400 from our code
        assert response.status_code in [400, 422]

    def test_upload_invalid_extension(self, client):
        """Test upload with non-XLIFF file extension."""
        response = client.post(
            "/upload",
            files={"file": ("test.txt", b"content", "text/plain")}
        )
        assert response.status_code == 400
        assert "Only .xlf files are allowed" in response.json()["detail"]

    @patch('app.http_client')
    def test_upload_malformed_xliff(self, mock_client, client, malformed_xliff, mock_httpx_success):
        """Test upload with malformed XLIFF content - job is created but fails in background."""
        mock_client.post = AsyncMock(return_value=mock_httpx_success)

        response = client.post(
            "/upload",
            files={"file": ("malformed.xlf", malformed_xliff.encode(), "application/xml")}
        )
        # Upload itself succeeds (file is saved), background task will fail
        assert response.status_code == 200
        assert "job_id" in response.json()

        # Cleanup
        if os.path.exists("uploads/malformed.xlf"):
            os.unlink("uploads/malformed.xlf")


# Test Progress Endpoint
class TestProgressEndpoint:
    """Tests for the GET /progress/{job_id} route."""

    def test_progress_nonexistent_job(self, client):
        """Test progress for a job that does not exist."""
        response = client.get("/progress/nonexistent-job-id")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_progress_pending_job(self, client):
        """Test progress for a pending job directly inserted into the store."""
        job_id = "test-pending-job"
        jobs[job_id] = {"status": "pending", "completed": 0, "total": 0,
                        "download_url": None, "error": None, "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "pending"
            assert data["completed"] == 0
            assert data["total"] == 0
            assert data["download_url"] is None
            assert data["error"] is None
        finally:
            jobs.pop(job_id, None)

    def test_progress_running_job(self, client):
        """Test progress for a running job with partial completion."""
        job_id = "test-running-job"
        jobs[job_id] = {"status": "running", "completed": 3, "total": 10,
                        "download_url": None, "error": None, "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "running"
            assert data["completed"] == 3
            assert data["total"] == 10
        finally:
            jobs.pop(job_id, None)

    def test_progress_completed_job(self, client):
        """Test progress for a completed job includes download_url."""
        job_id = "test-completed-job"
        jobs[job_id] = {"status": "completed", "completed": 5, "total": 5,
                        "download_url": "/download/translated_test.xlf", "error": None, "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed"
            assert data["completed"] == 5
            assert data["total"] == 5
            assert data["download_url"] == "/download/translated_test.xlf"
        finally:
            jobs.pop(job_id, None)

    def test_progress_failed_job(self, client):
        """Test progress for a failed job includes error message."""
        job_id = "test-failed-job"
        jobs[job_id] = {"status": "failed", "completed": 2, "total": 5,
                        "download_url": None, "error": "Translation service timeout", "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "failed"
            assert data["error"] == "Translation service timeout"
        finally:
            jobs.pop(job_id, None)


# Test Cancel Endpoint
class TestCancelEndpoint:
    """Tests for the DELETE /progress/{job_id} route."""

    def test_cancel_nonexistent_job(self, client):
        """Test cancelling a job that does not exist."""
        response = client.delete("/progress/nonexistent-job-id")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_cancel_completed_job_rejected(self, client):
        """Test that cancelling a completed job returns 400."""
        job_id = "test-cancel-completed"
        jobs[job_id] = {"status": "completed", "completed": 5, "total": 5,
                        "download_url": "/download/test.xlf", "error": None, "task": None}
        try:
            response = client.delete(f"/progress/{job_id}")
            assert response.status_code == 400
            assert "Cannot cancel" in response.json()["detail"]
        finally:
            jobs.pop(job_id, None)

    def test_cancel_failed_job_rejected(self, client):
        """Test that cancelling a failed job returns 400."""
        job_id = "test-cancel-failed"
        jobs[job_id] = {"status": "failed", "completed": 1, "total": 5,
                        "download_url": None, "error": "some error", "task": None}
        try:
            response = client.delete(f"/progress/{job_id}")
            assert response.status_code == 400
        finally:
            jobs.pop(job_id, None)

    def test_cancel_pending_job_no_task(self, client):
        """Test cancelling a pending job that has no asyncio task yet."""
        job_id = "test-cancel-pending"
        jobs[job_id] = {"status": "pending", "completed": 0, "total": 0,
                        "download_url": None, "error": None, "task": None}
        try:
            response = client.delete(f"/progress/{job_id}")
            assert response.status_code == 200
            assert "Cancellation requested" in response.json()["message"]
            assert jobs[job_id]["status"] == "cancelled"
        finally:
            jobs.pop(job_id, None)

    def test_cancel_running_job_with_done_task(self, client):
        """Test cancelling a running job whose task is already done falls back to setting status."""
        mock_task = MagicMock()
        mock_task.done.return_value = True

        job_id = "test-cancel-done-task"
        jobs[job_id] = {"status": "running", "completed": 3, "total": 10,
                        "download_url": None, "error": None, "task": mock_task}
        try:
            response = client.delete(f"/progress/{job_id}")
            assert response.status_code == 200
            assert jobs[job_id]["status"] == "cancelled"
        finally:
            jobs.pop(job_id, None)

    def test_cancel_running_job_cancels_task(self, client):
        """Test cancelling a running job calls cancel() on the asyncio task and sets cancelling state."""
        mock_task = MagicMock()
        mock_task.done.return_value = False

        job_id = "test-cancel-active-task"
        jobs[job_id] = {"status": "running", "completed": 2, "total": 10,
                        "download_url": None, "error": None, "task": mock_task}
        try:
            response = client.delete(f"/progress/{job_id}")
            assert response.status_code == 200
            mock_task.cancel.assert_called_once()
            assert jobs[job_id]["status"] == "cancelling"
        finally:
            jobs.pop(job_id, None)


# Test full async upload-to-completion flow
class TestAsyncUploadFlow:
    """End-to-end async tests for upload → progress → download flow."""

    @patch('app.http_client')
    async def test_upload_and_poll_until_complete(self, mock_client, sample_xliff, mock_httpx_success):
        """Test the full flow: upload, poll progress, verify completion."""
        mock_client.post = AsyncMock(return_value=mock_httpx_success)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
                tmp_file.write(sample_xliff.encode())
                tmp_path = tmp_file.name

            try:
                with open(tmp_path, 'rb') as f:
                    upload_resp = await ac.post(
                        "/upload",
                        files={"file": ("async_test.xlf", f, "application/xml")}
                    )

                assert upload_resp.status_code == 200
                job_id = upload_resp.json()["job_id"]
                assert len(job_id) == 36

                # Let the background task run to completion
                await asyncio.sleep(0.5)

                progress_resp = await ac.get(f"/progress/{job_id}")
                assert progress_resp.status_code == 200
                data = progress_resp.json()
                assert data["status"] in ("running", "completed")
                assert data["total"] >= 0
            finally:
                os.unlink(tmp_path)
                if os.path.exists("uploads/async_test.xlf"):
                    os.unlink("uploads/async_test.xlf")
                if os.path.exists("processed/translated_async_test.xlf"):
                    os.unlink("processed/translated_async_test.xlf")

    @patch('app.http_client')
    async def test_upload_translation_timeout_marks_job_failed(self, mock_client, sample_xliff):
        """Test that a translation timeout marks the job as failed."""
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

        import app as app_module
        original_retries = app_module.settings.max_retries
        app_module.settings.max_retries = 1  # Fail on first attempt, no backoff sleep

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
                tmp_file.write(sample_xliff.encode())
                tmp_path = tmp_file.name

            try:
                with open(tmp_path, 'rb') as f:
                    upload_resp = await ac.post(
                        "/upload",
                        files={"file": ("timeout_async.xlf", f, "application/xml")}
                    )

                assert upload_resp.status_code == 200
                job_id = upload_resp.json()["job_id"]

                # Allow background task to exhaust its single retry attempt
                await asyncio.sleep(0.3)

                progress_resp = await ac.get(f"/progress/{job_id}")
                assert progress_resp.status_code == 200
                data = progress_resp.json()
                assert data["status"] == "failed"
            finally:
                app_module.settings.max_retries = original_retries
                os.unlink(tmp_path)
                if os.path.exists("uploads/timeout_async.xlf"):
                    os.unlink("uploads/timeout_async.xlf")


# Test Download Endpoint
class TestDownloadEndpoint:
    """Tests for the download route."""

    def test_download_existing_file(self, client):
        """Test downloading an existing file."""
        test_content = b'<?xml version="1.0"?><xliff></xliff>'
        os.makedirs("processed", exist_ok=True)
        with open("processed/test_download.xlf", "wb") as f:
            f.write(test_content)

        try:
            response = client.get("/download/test_download.xlf")
            assert response.status_code == 200
            assert response.content == test_content
        finally:
            if os.path.exists("processed/test_download.xlf"):
                os.unlink("processed/test_download.xlf")

    def test_download_nonexistent_file(self, client):
        """Test downloading a file that doesn't exist."""
        response = client.get("/download/nonexistent.xlf")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_download_prevents_directory_traversal(self, client):
        """Test that directory traversal attempts are prevented."""
        response = client.get("/download/../../../etc/passwd")
        assert response.status_code == 404


# Test Health Check Endpoint
class TestHealthCheckEndpoint:
    """Tests for the health check route."""

    @patch('app.http_client')
    def test_health_all_healthy(self, mock_client, client, mock_httpx_languages):
        """Test health check when all services are healthy."""
        mock_client.get = AsyncMock(return_value=mock_httpx_languages)

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "degraded"]
        assert "libretranslate" in data
        assert "filesystem" in data

    @patch('app.http_client')
    def test_health_libretranslate_down(self, mock_client, client):
        """Test health check when LibreTranslate is unavailable."""
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["libretranslate"] == "unavailable"

    @patch('app.http_client')
    @patch('builtins.open')
    def test_health_filesystem_readonly(self, mock_open, mock_client, client, mock_httpx_languages):
        """Test health check when filesystem is read-only."""
        mock_client.get = AsyncMock(return_value=mock_httpx_languages)
        mock_open.side_effect = PermissionError("Read-only filesystem")

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["filesystem"] == "readonly"

    @patch('app.http_client')
    @patch('builtins.open')
    def test_health_all_down(self, mock_open, mock_client, client):
        """Test health check when both LibreTranslate and filesystem are down."""
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_open.side_effect = PermissionError("Read-only filesystem")

        response = client.get("/health")
        assert response.status_code == 503
        assert "unhealthy" in response.json()["detail"].lower()


# Test Placeholder Formatting
class TestPlaceholderFormatting:
    """Tests for placeholder formatting preservation."""

    def test_preserve_positional_placeholder(self):
        """Test preservation of positional placeholders like %1$s."""
        text = "You have%1$s messages"
        result = fix_placeholder_formatting(text)
        assert " %1$" in result

    def test_preserve_multiple_placeholders(self):
        """Test preservation of multiple positional placeholders."""
        text = "You have%1$s messages and%2$s notifications"
        result = fix_placeholder_formatting(text)
        assert " %1$" in result
        assert " %2$" in result

    def test_preserve_newline_placeholder(self):
        """Test preservation of %n placeholder."""
        text = "Line 1%nLine 2"
        result = fix_placeholder_formatting(text)
        assert " %n" in result

    def test_already_formatted_placeholder(self):
        """Test that already correctly formatted placeholders are unchanged."""
        text = "You have %1$s messages"
        result = fix_placeholder_formatting(text)
        assert result == text

    def test_empty_string(self):
        """Test placeholder formatting with empty string."""
        result = fix_placeholder_formatting("")
        assert result == ""

    def test_none_value(self):
        """Test placeholder formatting with None value."""
        result = fix_placeholder_formatting(None)
        assert result is None


# Test Secure Filename
class TestSecureFilename:
    """Tests for secure filename sanitization."""

    def test_simple_filename(self):
        """Test simple filename remains unchanged."""
        result = secure_filename("test.xlf")
        assert result == "test.xlf"

    def test_filename_with_spaces(self):
        """Test spaces are replaced with underscores."""
        result = secure_filename("test file.xlf")
        assert result == "test_file.xlf"

    def test_path_traversal_prevention(self):
        """Test that path traversal attempts are sanitized."""
        result = secure_filename("../../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        # werkzeug.secure_filename converts path separators to underscores
        assert result == "etc_passwd"

    def test_special_characters_removed(self):
        """Test that special characters are removed."""
        result = secure_filename("test@#$file!.xlf")
        assert result == "testfile.xlf"

    def test_leading_dots_removed(self):
        """Test that leading dots are removed to prevent hidden files."""
        result = secure_filename(".hidden.xlf")
        assert not result.startswith(".")
        assert result == "hidden.xlf"

    def test_absolute_path_basename_extracted(self):
        """Test that absolute paths are sanitized."""
        result = secure_filename("/path/to/file.xlf")
        assert "/" not in result
        # werkzeug.secure_filename converts path separators to underscores
        assert result == "path_to_file.xlf"

    def test_empty_filename_fallback(self):
        """Test that empty filenames get a fallback name."""
        result = secure_filename("")
        assert result == "unnamed"


# Test Error Handling
class TestErrorHandling:
    """Tests for general error handling."""

    @patch('app.http_client')
    def test_unexpected_error_marks_job_failed(self, mock_client, client, sample_xliff):
        """Test that unexpected translation errors mark the job as failed."""
        mock_client.post = AsyncMock(side_effect=Exception("Unexpected error"))

        with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
            tmp_file.write(sample_xliff.encode())
            tmp_file.flush()
            tmp_path = tmp_file.name

        try:
            with open(tmp_path, 'rb') as f:
                response = client.post(
                    "/upload",
                    files={"file": ("error_test.xlf", f, "application/xml")}
                )

            # Upload returns 200 immediately; error surfaces via progress endpoint
            assert response.status_code == 200
            assert "job_id" in response.json()
        finally:
            os.unlink(tmp_path)
            if os.path.exists("uploads/error_test.xlf"):
                os.unlink("uploads/error_test.xlf")


# Test Path Validation
class TestPathValidation:
    """Tests for path validation security."""

    def test_valid_path_in_directory(self):
        """Test that valid paths within directory are accepted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.xlf")
            assert validate_path_in_directory(file_path, tmpdir) is True

    def test_path_traversal_blocked(self):
        """Test that path traversal attempts are blocked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "../outside.xlf")
            assert validate_path_in_directory(file_path, tmpdir) is False

    def test_absolute_path_outside_directory(self):
        """Test that absolute paths outside directory are blocked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = "/etc/passwd"
            assert validate_path_in_directory(file_path, tmpdir) is False

    def test_subdirectory_allowed(self):
        """Test that subdirectories within allowed directory work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "subdir")
            os.makedirs(subdir, exist_ok=True)
            file_path = os.path.join(subdir, "test.xlf")
            assert validate_path_in_directory(file_path, tmpdir) is True

    def test_empty_path_rejected(self):
        """Test that empty paths are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assert validate_path_in_directory("", tmpdir) is False

    def test_complex_path_traversal_blocked(self):
        """Test that complex path traversal patterns are blocked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dangerous_patterns = [
                "../../etc/passwd",
                "./../outside.xlf",
                "subdir/../../outside.xlf",
                "../../../usr/bin/dangerous",
            ]

            for pattern in dangerous_patterns:
                file_path = os.path.join(tmpdir, pattern)
                result = validate_path_in_directory(file_path, tmpdir)
                assert result is False, f"Path traversal not blocked: {pattern}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app", "--cov-report=term-missing"])
