"""
Upload validation for XLIFF files: size limits and structure checks.

Every check here runs *before* the upload is written to disk and before a
translation job id is handed out, so a rejected upload leaves no file in
``uploads/`` and no job for the caller to poll.

The size limit is enforced twice on purpose:

1. On the declared ``Content-Length`` header, so an obviously oversized request
   is turned away without reading the body.
2. Again while reading, in bounded chunks, so a request with a missing,
   unparseable, or deliberately understated header cannot slip past. A cap
   applied after ``await file.read()`` would already have spent the memory it
   was meant to protect.

XML is parsed with ``defusedxml`` only. This module parses untrusted input, and
the codebase relies on defusedxml to prevent XXE and entity-expansion attacks.
"""
import logging
from typing import Optional, Protocol

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from fastapi import HTTPException

from settings import settings

logger = logging.getLogger(__name__)

# Bytes pulled from the upload stream per read. Bounds peak memory to roughly
# the limit plus one chunk, even for an arbitrarily large body.
CHUNK_SIZE = 64 * 1024

ROOT_ELEMENT = "xliff"
TRANS_UNIT_ELEMENT = "trans-unit"


class _Readable(Protocol):
    """Minimal async read interface shared by UploadFile and test doubles."""

    async def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes."""


def _resolve_limit(max_bytes: Optional[int]) -> int:
    """Return the effective limit, defaulting to the configured setting."""
    return settings.max_upload_bytes if max_bytes is None else max_bytes


def _too_large(limit: int) -> HTTPException:
    """Build the 413 raised whenever an upload exceeds the limit."""
    megabytes = limit / (1024 * 1024)
    return HTTPException(
        status_code=413,
        detail=(
            f"File too large. The maximum upload size is {limit} bytes "
            f"({megabytes:.2f} MB)."
        ),
    )


def _local_name(tag: object) -> str:
    """
    Return an element's tag without its XML namespace.

    ElementTree reports namespaced tags as ``{uri}local``. Real-world XLIFF 1.2
    files routinely declare ``urn:oasis:names:tc:xliff:document:1.2``, so
    comparisons are made on the local name to avoid rejecting valid documents.
    Comment and processing-instruction nodes carry a callable tag; those are
    reported as an empty name so they never match.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def enforce_content_length(
    content_length: Optional[str], max_bytes: Optional[int] = None
) -> None:
    """
    Reject an upload whose declared Content-Length already exceeds the limit.

    A missing or unparseable header is not an error here: it simply cannot be
    judged, and ``read_upload_within_limit`` remains the authoritative check.

    Args:
        content_length: Raw ``Content-Length`` header value, or None if absent.
        max_bytes: Override for the configured limit (mainly for tests).

    Raises:
        HTTPException: 413 if the declared length exceeds the limit.
    """
    limit = _resolve_limit(max_bytes)

    if content_length is None:
        return

    try:
        declared = int(content_length)
    except (TypeError, ValueError):
        logger.debug("Unparseable Content-Length header: %r", content_length)
        return

    if declared > limit:
        logger.warning(
            "Rejecting upload: declared Content-Length %d exceeds limit %d",
            declared,
            limit,
        )
        raise _too_large(limit)


async def read_upload_within_limit(
    file: _Readable, max_bytes: Optional[int] = None
) -> bytes:
    """
    Read an upload in bounded chunks, aborting as soon as the limit is passed.

    This is the check that holds when the ``Content-Length`` header is absent
    (chunked transfer encoding) or lying. Reading stops at the first chunk that
    pushes the running total over the limit, so at most ``limit + CHUNK_SIZE``
    bytes are ever held in memory regardless of how large the body actually is.

    Args:
        file: The uploaded file, or anything with an async ``read(size)``.
        max_bytes: Override for the configured limit (mainly for tests).

    Returns:
        The full upload contents.

    Raises:
        HTTPException: 413 if the body exceeds the limit.
    """
    limit = _resolve_limit(max_bytes)

    chunks: list[bytes] = []
    total = 0

    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break

        total += len(chunk)
        if total > limit:
            logger.warning(
                "Rejecting upload: body exceeded limit %d bytes while reading", limit
            )
            raise _too_large(limit)

        chunks.append(chunk)

    return b"".join(chunks)


def validate_xliff_structure(content: bytes) -> None:
    """
    Verify that the upload is a usable XLIFF document.

    Three checks, each with its own message so the caller learns which one
    failed: the bytes parse as XML, the root element is ``<xliff>``, and at
    least one ``<trans-unit>`` is present. Without this, any bytes named
    ``*.xlf`` would be written to disk and given a job id for work that was
    never viable.

    Args:
        content: Raw upload bytes.

    Raises:
        HTTPException: 422 naming the check that failed.
    """
    try:
        # defusedxml, never stdlib ElementTree: this is untrusted input.
        root = DET.fromstring(content)
    except DefusedXmlException as exc:
        logger.warning("Rejecting upload: unsafe XML constructs (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail=(
                "File rejected: the XML uses forbidden constructs "
                "(entities, DTDs, or external references)."
            ),
        ) from exc
    except DET.ParseError as exc:
        logger.warning("Rejecting upload: not well-formed XML (%s)", exc)
        raise HTTPException(
            status_code=422,
            detail=f"File is not valid XML: {exc}",
        ) from exc

    root_name = _local_name(root.tag)
    if root_name != ROOT_ELEMENT:
        logger.warning("Rejecting upload: root element is <%s>, expected <xliff>", root_name)
        raise HTTPException(
            status_code=422,
            detail=(
                f"File is valid XML but not XLIFF: root element is "
                f"<{root_name}>, expected <{ROOT_ELEMENT}>."
            ),
        )

    for element in root.iter():
        if _local_name(element.tag) == TRANS_UNIT_ELEMENT:
            return

    logger.warning("Rejecting upload: XLIFF contains no <trans-unit> elements")
    raise HTTPException(
        status_code=422,
        detail=f"XLIFF file contains no <{TRANS_UNIT_ELEMENT}> elements to translate.",
    )


async def validate_upload(
    file: _Readable,
    content_length: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> bytes:
    """
    Run every upload check and return the validated contents.

    Call this before writing to disk and before creating a job, so a rejected
    upload has no side effects.

    Args:
        file: The uploaded file.
        content_length: Raw ``Content-Length`` header value, or None if absent.
        max_bytes: Override for the configured limit (mainly for tests).

    Returns:
        The validated upload contents, ready to be written to disk.

    Raises:
        HTTPException: 413 if too large, 422 if not a usable XLIFF document.
    """
    enforce_content_length(content_length, max_bytes)
    content = await read_upload_within_limit(file, max_bytes)
    validate_xliff_structure(content)
    return content
