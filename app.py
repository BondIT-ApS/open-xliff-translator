import os
import re
import html
import uuid
import logging
import asyncio
import xml.etree.ElementTree as ET  # nosec B405 - Only used for writing XML, not parsing
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import defusedxml.ElementTree as DET
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from werkzeug.utils import secure_filename as werkzeug_secure_filename


# Settings configuration with Pydantic validation
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


# Load settings
settings = Settings()

# Configure logging
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Ensure directories exist
os.makedirs(settings.upload_folder, exist_ok=True)
os.makedirs(settings.processed_folder, exist_ok=True)

# Global HTTP client
http_client: Optional[httpx.AsyncClient] = None

# In-memory job store: job_id -> job state dict
jobs: Dict[str, Dict[str, Any]] = {}


# Pydantic models
class UploadResponse(BaseModel):
    message: str
    job_id: str


class ProgressResponse(BaseModel):
    status: str
    completed: int
    total: int
    download_url: Optional[str] = None
    error: Optional[str] = None


class HealthCheckResponse(BaseModel):
    status: str
    libretranslate: str
    filesystem: str


# Lifespan management
@asynccontextmanager
async def lifespan(
    _app: FastAPI,
):  # pylint: disable=redefined-outer-name,unused-argument
    """Manage application lifespan for httpx client initialization and cleanup."""
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
    logger.info("Application startup complete - HTTP client initialized")
    yield
    if http_client:
        await http_client.aclose()
    logger.info("Application shutdown complete - HTTP client closed")


# FastAPI app
app = FastAPI(
    title="Open XLIFF Translator",
    description="Dockerized web-based translation tool for XLIFF files using LibreTranslate",
    version="2.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")


# Utility functions
def secure_filename(filename: str) -> str:
    """
    Sanitize filename to prevent directory traversal attacks.
    Uses werkzeug's battle-tested secure_filename implementation.
    """
    sanitized = werkzeug_secure_filename(filename)
    if not sanitized:
        return "unnamed"
    return sanitized


def validate_path_in_directory(file_path: str, allowed_directory: str) -> bool:
    """
    Validate that a file path is within an allowed directory.
    Prevents path traversal attacks by checking the resolved absolute path.

    Args:
        file_path: The file path to validate
        allowed_directory: The directory that should contain the file

    Returns:
        True if the path is safe, False otherwise
    """
    try:
        # Resolve to absolute paths to handle symlinks and relative paths
        abs_file_path = os.path.abspath(file_path)
        abs_allowed_dir = os.path.abspath(allowed_directory)

        # Check if the file path starts with the allowed directory
        # Using os.path.commonpath to be extra safe
        common_path = os.path.commonpath([abs_file_path, abs_allowed_dir])
        return common_path == abs_allowed_dir
    except (ValueError, TypeError):
        # Handle edge cases like empty strings or invalid paths
        return False


def fix_placeholder_formatting(text: str) -> str:
    """Ensures placeholders like %1$s and %n remain correctly formatted with a leading space if needed.

    Retained for backward compatibility. Placeholder integrity is now primarily
    guaranteed by mask_placeholders/restore_placeholders, which prevent the
    translation engine from ever seeing (and thus corrupting) placeholders.
    """
    if text:
        text = re.sub(
            r"(?<!\s)%\s*(\d+)\s*\$", r" %\1$", text
        )  # Ensure space before %1$s
        text = re.sub(r"(?<!\s)%\s*n", r" %n", text)  # Ensure space before %n
    return text


# Ordered alternation of placeholder formats commonly found in XLIFF sources.
# More specific patterns must come first so they win during matching.
_PLACEHOLDER_PATTERN = re.compile(
    r"""
    (?P<ph>
        %%                              # escaped percent literal
      | %\d+\$[a-zA-Z@]                 # positional printf: %1$s, %2$d
      | %[-+0\#]?\d*(?:\.\d+)?[a-zA-Z@]  # printf: %s %d %.2f %02d %@ %n
      | \{\{[^{}]+\}\}                  # double-brace: {{var}}
      | \$\{[^{}]+\}                    # template literal: ${var}
      | \{[^{}]+\}                      # single-brace: {name}, {0}
    )
    """,
    re.VERBOSE,
)


def mask_placeholders(text: str) -> tuple[str, list[str]]:
    """
    Replace i18n placeholders with non-translatable HTML tags before
    translation. Combined with LibreTranslate's format="html" mode, the engine
    preserves the tags (and therefore the placeholders) instead of translating,
    reordering, or reformatting them.

    The literal text is HTML-escaped first so any &, < or > it contains cannot
    be misparsed as markup; placeholder patterns never contain those characters,
    so escaping does not affect matching.

    Returns (masked_html, originals) where originals[i] is the exact placeholder
    string replaced by the tag with index i.
    """
    originals: list[str] = []

    def _replace(match: re.Match) -> str:
        idx = len(originals)
        originals.append(match.group("ph"))
        return f"<x{idx}></x{idx}>"

    masked = _PLACEHOLDER_PATTERN.sub(_replace, html.escape(text, quote=False))
    return masked, originals


# Matches the placeholder tags produced by mask_placeholders (e.g. <x0></x0>,
# <x0/>, or a re-cased variant the engine may emit).
_SENTINEL_PATTERN = re.compile(r"<\s*/?\s*x\d+\s*/?>", re.IGNORECASE)


def restore_placeholders(text: str, originals: list[str]) -> str:
    """
    Restore placeholders previously masked by mask_placeholders and unescape the
    HTML produced by the translation engine.

    Tag matching tolerates the variations an engine may introduce: self-closing
    form (<x0/>), explicit pairs (<x0></x0>), re-casing, and incidental
    whitespace. Any unrecognised placeholder tags are stripped defensively so
    they never leak into the output.
    """
    for idx, original in enumerate(originals):
        pattern = re.compile(
            rf"<\s*x{idx}\s*/?>(?:\s*<\s*/\s*x{idx}\s*>)?",
            re.IGNORECASE,
        )
        # Function replacement keeps the original placeholder literal, so any
        # backslashes in it are never treated as regex backreferences.
        text = pattern.sub(lambda _match, value=original: value, text)

    # Strip any residual/unmapped placeholder tags before unescaping so they
    # never surface in the translated output.
    text = _SENTINEL_PATTERN.sub("", text)
    return html.unescape(text)


def has_translatable_text(masked_text: str) -> bool:
    """
    Report whether masked text still contains genuinely translatable content.

    Placeholder tags are removed first, then the remainder must contain a run of
    at least two letters. This avoids sending placeholder-only or near-empty
    strings (e.g. "%s", "%dm", "{count}") to the engine, which tends to drop or
    mangle short literals adjacent to a placeholder.
    """
    stripped = html.unescape(_SENTINEL_PATTERN.sub(" ", masked_text))
    return re.search(r"[^\W\d_]{2,}", stripped) is not None


# Translation functions
async def translate_text(text: str, target_lang: Optional[str] = None) -> str:
    """Translates text using LibreTranslate with retry logic."""
    if not text:
        return text

    if target_lang is None:
        target_lang = settings.default_target_language

    # Mask placeholders so the translation engine never sees (and cannot
    # corrupt) them; they are restored on the translated output below.
    masked_text, placeholders = mask_placeholders(text)

    # Nothing meaningful to translate (e.g. "%s", "%dm", "{count}"): return the
    # source unchanged instead of letting the engine drop short literals glued
    # to a placeholder or mangle the placeholder itself.
    if not has_translatable_text(masked_text):
        return text

    payload = {"q": masked_text, "source": "auto", "target": target_lang, "format": "html"}
    max_retries = settings.max_retries

    for attempt in range(max_retries):
        try:
            logger.debug(
                "Translation attempt %d/%d for text: %s...",
                attempt + 1,
                max_retries,
                text[:50],
            )
            response = await http_client.post(
                settings.libretranslate_url, json=payload, timeout=settings.http_timeout
            )
            response.raise_for_status()
            translated = response.json().get("translatedText", masked_text)
            translated = restore_placeholders(translated, placeholders)
            logger.debug("Translation successful: %s...", translated[:50])
            return translated
        except httpx.TimeoutException as e:
            logger.warning("LibreTranslate timeout on attempt %d: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                logger.error("LibreTranslate timeout after all retry attempts")
                raise HTTPException(
                    status_code=504, detail="Translation service timeout"
                ) from e
            await asyncio.sleep(2**attempt)  # Exponential backoff
        except httpx.HTTPStatusError as e:
            logger.error("LibreTranslate HTTP error on attempt %d: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                raise HTTPException(
                    status_code=502,
                    detail=f"Translation service error: {e.response.status_code}",
                ) from e
            await asyncio.sleep(2**attempt)
        except Exception as e:
            logger.error("Unexpected error during translation: %s", e)
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Translation failed") from e
            await asyncio.sleep(2**attempt)

    return text  # Fallback


async def translate_xliff_with_progress(
    job_id: str, input_file: str, output_file: str, target_lang: str
) -> None:
    """Parses an XLIFF file, translates each segment, and updates job progress in the jobs store."""
    jobs[job_id]["status"] = "running"
    try:
        logger.info("Job %s: starting translation %s -> %s", job_id, input_file, output_file)
        tree = DET.parse(input_file)  # Securely parse XML
        root = tree.getroot()

        trans_units = root.findall(".//trans-unit")
        jobs[job_id]["total"] = len(trans_units)
        logger.info("Job %s: found %d translation units", job_id, len(trans_units))

        for idx, trans_unit in enumerate(trans_units):
            if jobs[job_id]["status"] in ("cancelled", "cancelling"):
                jobs[job_id]["status"] = "cancelled"
                logger.info("Job %s: cancelled at unit %d/%d", job_id, idx + 1, len(trans_units))
                return

            source = trans_unit.find("source")
            target = trans_unit.find("target")

            if source is not None and source.text:
                translated_text = await translate_text(source.text, target_lang)

                if target is None:
                    target = ET.SubElement(trans_unit, "target")  # Ensure Transifex compatibility
                target.text = translated_text
                target.set("state", "needs-review-translation")  # Set state for Transifex validation

            jobs[job_id]["completed"] = idx + 1

        new_tree = ET.ElementTree(root)
        new_tree.write(output_file, encoding="utf-8", xml_declaration=True)
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["download_url"] = f"/download/{os.path.basename(output_file)}"
        logger.info("Job %s: completed successfully", job_id)

    except asyncio.CancelledError:
        jobs[job_id]["status"] = "cancelled"
        logger.info("Job %s: was cancelled", job_id)
        raise
    except HTTPException as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = e.detail
        logger.error("Job %s: failed with HTTP error: %s", job_id, e.detail)
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = f"XLIFF processing failed: {str(e)}"
        logger.error("Job %s: failed with unexpected error: %s", job_id, e)


# Routes
@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main upload interface."""
    try:
        with open("templates/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError as exc:
        logger.error("index.html template not found")
        raise HTTPException(status_code=500, detail="Template not found") from exc


@app.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)):
    """Handle XLIFF file upload and start background translation."""
    if not file:
        logger.warning("Upload request with no file")
        raise HTTPException(status_code=400, detail="No file part")

    if not file.filename:
        logger.warning("Upload request with empty filename")
        raise HTTPException(status_code=400, detail="No selected file")

    if not file.filename.endswith(".xlf"):
        logger.warning("Invalid file extension: %s", file.filename)
        raise HTTPException(status_code=400, detail="Only .xlf files are allowed")

    filename = secure_filename(file.filename)
    file_path = os.path.join(settings.upload_folder, filename)

    # Validate path to prevent directory traversal
    if not validate_path_in_directory(file_path, settings.upload_folder):
        logger.error("Path traversal attempt detected: %s", file_path)
        raise HTTPException(status_code=400, detail="Invalid file path")

    try:
        # Save uploaded file
        logger.info("Saving uploaded file: %s", filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        translated_filename = secure_filename(f"translated_{filename}")
        output_file = os.path.join(settings.processed_folder, translated_filename)

        # Validate output path to prevent directory traversal
        if not validate_path_in_directory(output_file, settings.processed_folder):
            logger.error("Path traversal attempt in output path: %s", output_file)
            raise HTTPException(status_code=400, detail="Invalid output path")

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "pending",
            "completed": 0,
            "total": 0,
            "download_url": None,
            "error": None,
            "task": None,
        }
        task = asyncio.create_task(
            translate_xliff_with_progress(job_id, file_path, output_file, settings.default_target_language)
        )
        jobs[job_id]["task"] = task

        logger.info("Started translation job %s for file: %s", job_id, filename)
        return UploadResponse(message="Translation started", job_id=job_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing upload: %s", e)
        raise HTTPException(
            status_code=500, detail=f"File processing failed: {str(e)}"
        ) from e


@app.get("/progress/{job_id}", response_model=ProgressResponse)
async def get_progress(job_id: str):
    """Get the current progress of a translation job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return ProgressResponse(
        status=job["status"],
        completed=job["completed"],
        total=job["total"],
        download_url=job.get("download_url"),
        error=job.get("error"),
    )


@app.delete("/progress/{job_id}")
async def cancel_job(job_id: str):
    """Cancel an in-progress translation job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job in state: {job['status']}")

    task = job.get("task")
    if task and not task.done():
        job["status"] = "cancelling"
        task.cancel()
    else:
        job["status"] = "cancelled"

    logger.info("Cancellation requested for job %s", job_id)
    return JSONResponse(content={"message": "Cancellation requested"})


@app.get("/download/{filename}")
async def download_file(filename: str):
    """Download translated XLIFF file."""
    safe_filename = secure_filename(filename)
    file_path = os.path.join(settings.processed_folder, safe_filename)

    # Validate path to prevent directory traversal
    if not validate_path_in_directory(file_path, settings.processed_folder):
        logger.error("Path traversal attempt in download: %s", file_path)
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Ensure the file exists before attempting to send it
    if not os.path.exists(file_path):
        logger.warning("Download requested for non-existent file: %s", safe_filename)
        raise HTTPException(status_code=404, detail="File not found")

    logger.info("Serving file for download: %s", safe_filename)
    return FileResponse(
        path=file_path, filename=safe_filename, media_type="application/xml"
    )


@app.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """Health check endpoint for monitoring."""
    status = "healthy"
    libretranslate_status = "unavailable"
    filesystem_status = "readonly"

    # Check LibreTranslate
    try:
        response = await http_client.get(
            settings.libretranslate_languages_url, timeout=5.0
        )
        if response.status_code == 200:
            libretranslate_status = "available"
            logger.debug("LibreTranslate health check passed")
        else:
            status = "degraded"
            logger.warning(
                "LibreTranslate health check failed: %s", response.status_code
            )
    except Exception as e:
        status = "degraded"
        logger.warning("LibreTranslate health check exception: %s", e)

    # Check filesystem
    try:
        # Test write to uploads
        test_file_uploads = os.path.join(settings.upload_folder, ".health_check")
        with open(test_file_uploads, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file_uploads)

        # Test write to processed
        test_file_processed = os.path.join(settings.processed_folder, ".health_check")
        with open(test_file_processed, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file_processed)

        filesystem_status = "writable"
        logger.debug("Filesystem health check passed")
    except Exception as e:
        status = "degraded"
        logger.warning("Filesystem health check failed: %s", e)

    # Determine overall status
    if libretranslate_status == "unavailable" and filesystem_status == "readonly":
        status = "unhealthy"
        logger.error(
            "Health check failed: both LibreTranslate and filesystem unavailable"
        )
        raise HTTPException(status_code=503, detail="Service unhealthy")

    logger.info(
        "Health check: %s (LibreTranslate: %s, Filesystem: %s)",
        status,
        libretranslate_status,
        filesystem_status,
    )
    return HealthCheckResponse(
        status=status,
        libretranslate=libretranslate_status,
        filesystem=filesystem_status,
    )


if __name__ == "__main__":
    import uvicorn

    # Bind to 0.0.0.0 for Docker container accessibility
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)  # nosec B104
