"""
Comprehensive test suite for Open XLIFF Translator FastAPI application.
"""
import os
import tempfile
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import httpx
from fastapi.testclient import TestClient
from app import app, secure_filename, fix_placeholder_formatting, validate_path_in_directory

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
    def test_upload_valid_file(self, mock_client, client, sample_xliff, mock_httpx_success):
        """Test successful upload and translation of valid XLIFF file."""
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
            assert "download_url" in data
            assert "translated_test.xlf" in data["download_url"]
        finally:
            os.unlink(tmp_path)
            # Cleanup uploaded and processed files
            if os.path.exists("uploads/test.xlf"):
                os.unlink("uploads/test.xlf")
            if os.path.exists("processed/translated_test.xlf"):
                os.unlink("processed/translated_test.xlf")

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
        """Test upload with malformed XLIFF content."""
        mock_client.post = AsyncMock(return_value=mock_httpx_success)

        response = client.post(
            "/upload",
            files={"file": ("malformed.xlf", malformed_xliff.encode(), "application/xml")}
        )
        assert response.status_code == 500
        assert "processing failed" in response.json()["detail"].lower()

        # Cleanup
        if os.path.exists("uploads/malformed.xlf"):
            os.unlink("uploads/malformed.xlf")

    @patch('app.http_client')
    def test_upload_translation_timeout(self, mock_client, client, sample_xliff):
        """Test upload with LibreTranslate timeout."""
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

        with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
            tmp_file.write(sample_xliff.encode())
            tmp_file.flush()
            tmp_path = tmp_file.name

        try:
            with open(tmp_path, 'rb') as f:
                response = client.post(
                    "/upload",
                    files={"file": ("timeout_test.xlf", f, "application/xml")}
                )

            assert response.status_code == 504
            assert "timeout" in response.json()["detail"].lower()
        finally:
            os.unlink(tmp_path)
            if os.path.exists("uploads/timeout_test.xlf"):
                os.unlink("uploads/timeout_test.xlf")


# Test Download Endpoint
class TestDownloadEndpoint:
    """Tests for the download route."""

    def test_download_existing_file(self, client):
        """Test downloading an existing file."""
        # Create a test file
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
        # Should return degraded, not unhealthy (filesystem still works)
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
        # Should return degraded (LibreTranslate works)
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
    def test_unexpected_error_returns_500(self, mock_client, client, sample_xliff):
        """Test that unexpected errors return 500 status."""
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

            assert response.status_code == 500
        finally:
            os.unlink(tmp_path)
            if os.path.exists("uploads/error_test.xlf"):
                os.unlink("uploads/error_test.xlf")

    @patch('app.http_client')
    def test_http_status_error_handling(self, mock_client, client, sample_xliff):
        """Test handling of HTTP status errors from LibreTranslate."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=mock_response
        )
        mock_client.post = AsyncMock(return_value=mock_response)

        with tempfile.NamedTemporaryFile(suffix='.xlf', delete=False) as tmp_file:
            tmp_file.write(sample_xliff.encode())
            tmp_file.flush()
            tmp_path = tmp_file.name

        try:
            with open(tmp_path, 'rb') as f:
                response = client.post(
                    "/upload",
                    files={"file": ("status_error.xlf", f, "application/xml")}
                )

            assert response.status_code in [500, 502]
        finally:
            os.unlink(tmp_path)
            if os.path.exists("uploads/status_error.xlf"):
                os.unlink("uploads/status_error.xlf")


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
            # Try to escape using ../
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
            # Test various path traversal patterns
            dangerous_patterns = [
                "../../etc/passwd",
                "./../outside.xlf",
                "subdir/../../outside.xlf",
                "../../../usr/bin/dangerous",
            ]

            for pattern in dangerous_patterns:
                file_path = os.path.join(tmpdir, pattern)
                # All of these should be blocked
                result = validate_path_in_directory(file_path, tmpdir)
                assert result is False, f"Path traversal not blocked: {pattern}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app", "--cov-report=term-missing"])
