"""
Tests for upload hardening: size limits and XLIFF structure validation (issue #145).

Covers both the ``validation`` module in isolation and its enforcement through the
``POST /upload`` route, including the cases where the ``Content-Length`` header is
absent or actively lying about the body size.
"""
import io
import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from app import app
from settings import settings
from translation import jobs
import validation


VALID_XLIFF = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
    <file source-language="en" target-language="da">
        <body>
            <trans-unit id="1">
                <source>Hello World</source>
            </trans-unit>
        </body>
    </file>
</xliff>"""

NAMESPACED_XLIFF = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">
    <file source-language="en" target-language="da" datatype="plaintext" original="a">
        <body>
            <trans-unit id="1">
                <source>Hello World</source>
            </trans-unit>
        </body>
    </file>
</xliff>"""

XLIFF_WITHOUT_TRANS_UNITS = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
    <file source-language="en" target-language="da">
        <body>
        </body>
    </file>
</xliff>"""

VALID_XML_NOT_XLIFF = b"""<?xml version="1.0" encoding="UTF-8"?>
<resources>
    <string name="greeting">Hello World</string>
</resources>"""

NOT_XML_AT_ALL = b"this is definitely not xml, it is just some bytes {\x00\x01}"

XXE_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE xliff [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<xliff version="1.2">
    <file><body><trans-unit id="1"><source>&xxe;</source></trans-unit></body></file>
</xliff>"""


@pytest.fixture
def small_limit(monkeypatch):
    """Shrink the upload limit so tests do not have to build 50 MB bodies."""
    monkeypatch.setattr(settings, "max_upload_bytes", 2048)
    return 2048


@pytest.fixture
def client():
    """Test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def uploads_snapshot():
    """Snapshot the uploads directory so tests can assert nothing was written."""
    os.makedirs(settings.upload_folder, exist_ok=True)
    return set(os.listdir(settings.upload_folder))


def make_upload(data: bytes, filename: str = "test.xlf") -> UploadFile:
    """Build an UploadFile backed by an in-memory buffer."""
    return UploadFile(file=io.BytesIO(data), filename=filename)


def multipart_body(content: bytes, filename: str = "test.xlf", boundary: str = "----xliffbound"):
    """Build a raw multipart/form-data body and its content type header."""
    head = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/xml\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class TestMaxUploadBytesSetting:
    """The limit must exist with a 50 MB default and be environment-configurable."""

    def test_default_is_fifty_megabytes(self):
        """Default limit is 50 MB."""
        assert settings.__class__.model_fields["max_upload_bytes"].default == 50 * 1024 * 1024

    def test_configurable_via_environment(self, monkeypatch):
        """MAX_UPLOAD_BYTES overrides the default."""
        monkeypatch.setenv("MAX_UPLOAD_BYTES", "12345")
        assert settings.__class__(_env_file=None).max_upload_bytes == 12345

    def test_documented_in_env_template(self):
        """The setting is documented in .env.template."""
        with open(".env.template", "r", encoding="utf-8") as handle:
            assert "MAX_UPLOAD_BYTES" in handle.read()


# ---------------------------------------------------------------------------
# Size limit: Content-Length header check
# ---------------------------------------------------------------------------
class TestContentLengthCheck:
    """Declared Content-Length is rejected up front when it exceeds the limit."""

    def test_over_limit_rejected_with_413(self, small_limit):
        """A declared length above the limit raises 413."""
        with pytest.raises(HTTPException) as exc:
            validation.enforce_content_length(str(small_limit + 1))
        assert exc.value.status_code == 413

    def test_413_message_names_the_limit(self, small_limit):
        """The 413 detail states the configured limit."""
        with pytest.raises(HTTPException) as exc:
            validation.enforce_content_length(str(small_limit + 1))
        assert str(small_limit) in exc.value.detail

    def test_at_limit_accepted(self, small_limit):
        """A declared length exactly at the limit is allowed."""
        validation.enforce_content_length(str(small_limit))

    def test_absent_header_is_not_rejected_here(self, small_limit):
        """A missing header cannot be judged; the streaming check is the real gate."""
        validation.enforce_content_length(None)

    def test_unparseable_header_is_not_rejected_here(self, small_limit):
        """A garbage header cannot be judged; the streaming check is the real gate."""
        validation.enforce_content_length("not-a-number")


# ---------------------------------------------------------------------------
# Size limit: streaming check
# ---------------------------------------------------------------------------
class TestStreamingSizeCheck:
    """The limit is enforced again while reading, independent of any header."""

    async def test_under_limit_returns_full_content(self, small_limit):
        """Content below the limit is returned intact."""
        payload = b"a" * 100
        assert await validation.read_upload_within_limit(make_upload(payload)) == payload

    async def test_at_limit_accepted(self, small_limit):
        """Content exactly at the limit is accepted."""
        payload = b"a" * small_limit
        assert len(await validation.read_upload_within_limit(make_upload(payload))) == small_limit

    async def test_over_limit_raises_413(self, small_limit):
        """One byte over the limit raises 413."""
        with pytest.raises(HTTPException) as exc:
            await validation.read_upload_within_limit(make_upload(b"a" * (small_limit + 1)))
        assert exc.value.status_code == 413
        assert str(small_limit) in exc.value.detail

    async def test_reads_in_chunks_and_stops_early(self, small_limit):
        """The read is chunked and abandoned once the limit is passed.

        Guards against a cap applied after ``await file.read()`` has already spent
        the memory it was meant to protect.
        """
        handed_out = []

        class CountingUpload:
            """Stub upload that records how much data it hands out per read."""

            def __init__(self, total: int):
                self.remaining = total

            async def read(self, size: int = -1) -> bytes:
                assert size not in (-1, None), "read() must request a bounded chunk"
                chunk = min(size, self.remaining)
                self.remaining -= chunk
                handed_out.append(chunk)
                return b"a" * chunk

        # 100x the limit available, but the reader must bail out almost immediately.
        with pytest.raises(HTTPException) as exc:
            await validation.read_upload_within_limit(CountingUpload(small_limit * 100))

        assert exc.value.status_code == 413
        assert sum(handed_out) <= small_limit + validation.CHUNK_SIZE
        assert max(handed_out) <= validation.CHUNK_SIZE


# ---------------------------------------------------------------------------
# Structure validation
# ---------------------------------------------------------------------------
class TestXliffStructureValidation:
    """Structure is validated with defusedxml before the file is trusted."""

    def test_valid_xliff_accepted(self):
        """A well-formed XLIFF with trans-units passes."""
        validation.validate_xliff_structure(VALID_XLIFF)

    def test_namespaced_xliff_accepted(self):
        """A namespaced XLIFF 1.2 document passes."""
        validation.validate_xliff_structure(NAMESPACED_XLIFF)

    def test_non_xml_rejected_with_422(self):
        """Bytes that are not XML at all are rejected."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(NOT_XML_AT_ALL)
        assert exc.value.status_code == 422
        assert "XML" in exc.value.detail

    def test_empty_content_rejected_with_422(self):
        """An empty upload is rejected."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(b"")
        assert exc.value.status_code == 422

    def test_truncated_xml_rejected_with_422(self):
        """Unclosed tags are rejected."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(b"<xliff><file><body><trans-unit>")
        assert exc.value.status_code == 422

    def test_valid_xml_that_is_not_xliff_rejected_with_422(self):
        """A well-formed XML document with the wrong root is rejected."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(VALID_XML_NOT_XLIFF)
        assert exc.value.status_code == 422
        assert "xliff" in exc.value.detail.lower()
        assert "resources" in exc.value.detail

    def test_xliff_without_trans_units_rejected_with_422(self):
        """An XLIFF file containing no trans-unit is rejected."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(XLIFF_WITHOUT_TRANS_UNITS)
        assert exc.value.status_code == 422
        assert "trans-unit" in exc.value.detail

    def test_failure_message_distinguishes_which_check_failed(self):
        """Each failure mode produces a distinct message."""
        details = []
        for payload in (NOT_XML_AT_ALL, VALID_XML_NOT_XLIFF, XLIFF_WITHOUT_TRANS_UNITS):
            with pytest.raises(HTTPException) as exc:
                validation.validate_xliff_structure(payload)
            details.append(exc.value.detail)
        assert len(set(details)) == 3

    def test_entity_expansion_rejected_not_resolved(self):
        """An XXE payload is refused by defusedxml rather than resolved."""
        with pytest.raises(HTTPException) as exc:
            validation.validate_xliff_structure(XXE_PAYLOAD)
        assert exc.value.status_code == 422
        assert "/etc/passwd" not in exc.value.detail

    def test_non_string_tags_never_match(self):
        """Comment and processing-instruction nodes carry a callable tag."""
        assert validation._local_name(lambda *a: None) == ""
        assert validation._local_name("{urn:oasis:names:tc:xliff:document:1.2}xliff") == "xliff"
        assert validation._local_name("trans-unit") == "trans-unit"

    def test_parses_with_defusedxml(self):
        """Parsing goes through defusedxml, never stdlib ElementTree."""
        with open("validation.py", "r", encoding="utf-8") as handle:
            source = handle.read()
        assert "defusedxml" in source
        assert "import xml.etree" not in source


# ---------------------------------------------------------------------------
# Route enforcement
# ---------------------------------------------------------------------------
class TestUploadRouteSizeEnforcement:
    """POST /upload rejects oversized bodies without side effects."""

    def test_over_limit_rejected_with_413(self, client, small_limit, uploads_snapshot):
        """An oversized upload gets 413 and leaves nothing behind."""
        jobs_before = set(jobs)
        response = client.post(
            "/upload",
            files={"file": ("big.xlf", b"a" * (small_limit * 4), "application/xml")},
        )

        assert response.status_code == 413
        assert str(small_limit) in response.json()["detail"]
        assert "job_id" not in response.json()
        assert set(jobs) == jobs_before
        assert set(os.listdir(settings.upload_folder)) == uploads_snapshot

    async def test_limit_enforced_when_content_length_lies(self, small_limit, uploads_snapshot):
        """A body far over the limit behind a tiny Content-Length is still rejected."""
        jobs_before = set(jobs)
        oversized = b"a" * (small_limit * 4)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post(
                "/upload",
                files={"file": ("liar.xlf", oversized, "application/xml")},
                headers={"content-length": "10"},
            )

        # The header the app saw really was the lie, not the true body size.
        assert response.status_code == 413
        assert str(small_limit) in response.json()["detail"]
        assert set(jobs) == jobs_before
        assert set(os.listdir(settings.upload_folder)) == uploads_snapshot

    async def test_limit_enforced_when_content_length_absent(self, small_limit, uploads_snapshot):
        """A chunked upload with no Content-Length at all is still rejected."""
        jobs_before = set(jobs)
        body, content_type = multipart_body(b"a" * (small_limit * 4), filename="chunked.xlf")

        async def stream():
            for start in range(0, len(body), 512):
                yield body[start:start + 512]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/upload", content=stream(), headers={"content-type": content_type})

        assert response.status_code == 413
        assert set(jobs) == jobs_before
        assert set(os.listdir(settings.upload_folder)) == uploads_snapshot

    async def test_lying_header_actually_reaches_the_app_as_a_lie(self, small_limit):
        """Sanity check: the transport really does forward the understated header."""
        seen = {}
        original = validation.enforce_content_length

        def spy(content_length, max_bytes=None):
            seen["content_length"] = content_length
            return original(content_length, max_bytes)

        # validate_upload resolves enforce_content_length through the module global,
        # so the spy is picked up by the real request path.
        with patch.object(validation, "enforce_content_length", spy):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post(
                    "/upload",
                    files={"file": ("liar.xlf", b"a" * (small_limit * 4), "application/xml")},
                    headers={"content-length": "10"},
                )

        assert seen["content_length"] == "10"


class TestUploadRouteStructureEnforcement:
    """POST /upload rejects structurally invalid XLIFF before creating a job."""

    @pytest.mark.parametrize(
        "name,payload",
        [
            ("not_xml.xlf", NOT_XML_AT_ALL),
            ("not_xliff.xlf", VALID_XML_NOT_XLIFF),
            ("no_units.xlf", XLIFF_WITHOUT_TRANS_UNITS),
        ],
    )
    def test_invalid_structure_rejected_with_422(self, client, uploads_snapshot, name, payload):
        """Each invalid shape is rejected with 422, no job and no file."""
        jobs_before = set(jobs)
        response = client.post("/upload", files={"file": (name, payload, "application/xml")})

        assert response.status_code == 422
        assert "job_id" not in response.json()
        assert set(jobs) == jobs_before
        assert set(os.listdir(settings.upload_folder)) == uploads_snapshot
        assert not os.path.exists(os.path.join(settings.upload_folder, name))

    def test_422_detail_says_which_check_failed(self, client, uploads_snapshot):
        """The 422 body explains the specific failure."""
        response = client.post(
            "/upload", files={"file": ("no_units.xlf", XLIFF_WITHOUT_TRANS_UNITS, "application/xml")}
        )
        assert response.status_code == 422
        assert "trans-unit" in str(response.json()["detail"])

    @patch("translation.http_client")
    def test_valid_upload_still_succeeds(self, mock_client, client, uploads_snapshot):
        """A valid XLIFF file is still accepted and gets a job id."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"translatedText": "Hej Verden"}
        mock_client.post = AsyncMock(return_value=mock_response)

        try:
            response = client.post(
                "/upload", files={"file": ("valid_145.xlf", VALID_XLIFF, "application/xml")}
            )
            assert response.status_code == 200
            assert len(response.json()["job_id"]) == 36
            assert os.path.exists(os.path.join(settings.upload_folder, "valid_145.xlf"))
        finally:
            leftover = os.path.join(settings.upload_folder, "valid_145.xlf")
            if os.path.exists(leftover):
                os.unlink(leftover)
