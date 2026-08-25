import os
import uuid
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from werkzeug.utils import secure_filename as werkzeug_secure_filename

import cleanup
import db
import glossary
import translation
from settings import settings
from translation import jobs, translate_xliff_with_progress
from validation import validate_upload

# Configure logging
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Ensure directories exist
os.makedirs(settings.upload_folder, exist_ok=True)
os.makedirs(settings.processed_folder, exist_ok=True)


# Pydantic models
class UploadResponse(BaseModel):
    message: str
    job_id: str


class ProgressResponse(BaseModel):
    status: str
    completed: int
    total: int
    terms_applied: int = 0
    download_url: Optional[str] = None
    error: Optional[str] = None


class HealthCheckResponse(BaseModel):
    status: str
    libretranslate: str
    filesystem: str
    database: str


# Lifespan management
@asynccontextmanager
async def lifespan(
    _app: FastAPI,
):  # pylint: disable=redefined-outer-name,unused-argument
    """Manage application lifespan for HTTP client and database setup and cleanup."""
    await translation.startup_http_client()
    db.startup_database()
    await cleanup.start_cleanup_task()
    logger.info("Application startup complete")
    yield
    await cleanup.stop_cleanup_task()
    db.shutdown_database()
    await translation.shutdown_http_client()
    logger.info("Application shutdown complete")


# FastAPI app
app = FastAPI(
    title="Open XLIFF Translator",
    description="Dockerized web-based translation tool for XLIFF files using LibreTranslate",
    version="2.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")

# The UI's CSS and JS are served from /static rather than inlined, so a
# Content-Security-Policy can keep script-src/style-src at 'self'.
app.mount("/static", StaticFiles(directory="static"), name="static")


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
async def upload_file(request: Request, file: UploadFile = File(...)):
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

    # Enforce the size limit and validate XLIFF structure before anything is
    # written to uploads/ and before a job id exists, so a rejected upload
    # leaves no file and nothing for the caller to poll.
    content = await validate_upload(file, request.headers.get("content-length"))

    try:
        # Save validated file
        logger.info("Saving uploaded file: %s", filename)
        with open(file_path, "wb") as f:
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
            "terms_applied": 0,
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
        terms_applied=job.get("terms_applied", 0),
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
    database_status = "error"

    # Check LibreTranslate
    try:
        response = await translation.http_client.get(
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

    # Check database
    try:
        db.get_connection().execute("SELECT 1").fetchone()
        database_status = "ok"
        logger.debug("Database health check passed")
    except Exception as e:  # pylint: disable=broad-except
        status = "degraded"
        logger.warning("Database health check failed: %s", e)

    # Determine overall status
    if libretranslate_status == "unavailable" and filesystem_status == "readonly":
        status = "unhealthy"
        logger.error(
            "Health check failed: both LibreTranslate and filesystem unavailable"
        )
        raise HTTPException(status_code=503, detail="Service unhealthy")

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


# ── Download history (issue #28) ─────────────────────────────────────────────
# Identity is an anonymous session cookie: an opaque random id scoped to the
# browser, no accounts and no login. It says which history to show and nothing
# else — it is never an authentication credential.
#
# The surface is read-only on purpose: list, and re-download through the
# existing /download route. There is no delete endpoint, so a forged
# cross-site request has nothing to act on. Adding one later would make real
# CSRF protection mandatory (see the decision recorded on issue #28).
#
# Recording lives here rather than in the upload handler so that a row exists
# from the moment the job does — history has to survive the browser closing
# mid-translation — and so nothing about a row is ever taken from a request
# body. Imports are local to this block to keep the feature self-contained.
import json  # pylint: disable=wrong-import-position,wrong-import-order

# pylint: disable=wrong-import-position,wrong-import-order,ungrouped-imports
from fastapi import Request, Response

import history  # pylint: disable=wrong-import-position


class HistoryEntry(BaseModel):
    """One past translation belonging to the calling session."""

    id: int
    job_id: str
    original_filename: Optional[str] = None
    translated_filename: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    expires_at: str
    file_size: Optional[int] = None
    source_language: str
    target_language: str
    status: str
    download_url: Optional[str] = None


class HistoryResponse(BaseModel):
    entries: list[HistoryEntry]
    retention_days: int


def history_processed_path(filename: str) -> Optional[str]:
    """
    Resolve a stored filename to a path inside the processed folder, or None.

    Applies exactly the checks the download route applies, so a history entry
    is only ever linked when /download would actually serve that same file.
    """
    if not filename:
        return None
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return None
    file_path = os.path.join(settings.processed_folder, safe_filename)
    if not validate_path_in_directory(file_path, settings.processed_folder):
        return None
    return file_path


@app.middleware("http")
async def history_session_middleware(request: Request, call_next):
    """
    Issue the anonymous session cookie and keep history in step with jobs.

    A started translation is recorded against the uploading session, and the
    outcome of in-flight jobs is persisted while the UI polls progress, so a
    finished translation is durable long before the tab is closed. History is
    strictly best-effort: any failure here is logged and never propagated to
    the request it rides along with.
    """
    session_id = request.cookies.get(history.SESSION_COOKIE_NAME)
    issue_cookie = not history.is_valid_session_id(session_id)
    if issue_cookie:
        session_id = history.new_session_id()

    response = await call_next(request)
    path = request.url.path

    if request.method == "POST" and path == "/upload" and response.status_code == 200:
        # The job id is only known from the response, so it is buffered and
        # re-sent unchanged. Gated on this one small JSON route: streaming
        # responses such as file downloads are passed through untouched.
        body = b"".join([chunk async for chunk in response.body_iterator])
        response = Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        try:
            job_id = json.loads(body).get("job_id")
            if job_id:
                history.record_job(
                    db.get_connection(),
                    session_id,
                    job_id,
                    settings.default_target_language,
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Could not record translation history: %s", e)
    elif path.startswith("/progress"):
        try:
            history.reconcile(db.get_connection(), jobs, settings.processed_folder)
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("History reconciliation skipped: %s", e)

    if issue_cookie:
        history.set_session_cookie(
            response,
            session_id,
            secure=request.url.scheme == "https",
            retention_days=settings.file_retention_days,
        )
    return response


@app.get("/api/history", response_model=HistoryResponse)
async def get_history(request: Request):
    """List this session's translations. Re-download uses /download/{filename}."""
    retention_days = settings.file_retention_days
    session_id = request.cookies.get(history.SESSION_COOKIE_NAME)
    if not history.is_valid_session_id(session_id):
        # No session yet: the middleware is issuing one with this response.
        return HistoryResponse(entries=[], retention_days=retention_days)

    try:
        connection = db.get_connection()
        history.reconcile(connection, jobs, settings.processed_folder)
        entries = history.list_history(
            connection,
            session_id,
            retention_days=retention_days,
            resolve_path=history_processed_path,
        )
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("History listing unavailable: %s", e)
        entries = []

    return HistoryResponse(entries=entries, retention_days=retention_days)


# ── End download history ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
# Vocabulary overrides (issue #142). Models and routes are kept together as one
# contiguous block so the feature is easy to review, move, or revert.
# ---------------------------------------------------------------------------
class TermIn(BaseModel):
    target_lang: str = "da"
    source_term: str
    target_term: str
    match_case: bool = False
    enabled: bool = True
    note: Optional[str] = None


class TermUpdate(BaseModel):
    target_lang: Optional[str] = None
    source_term: Optional[str] = None
    target_term: Optional[str] = None
    match_case: Optional[bool] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


class TermOut(BaseModel):
    id: int
    target_lang: str
    source_term: str
    target_term: str
    match_case: bool
    enabled: bool
    note: Optional[str] = None


@app.get("/api/glossary", response_model=List[TermOut])
async def list_glossary(target_lang: Optional[str] = None):
    """List vocabulary overrides, optionally filtered by target language."""
    terms = glossary.list_terms(db.get_connection(), target_lang)
    return [TermOut(**t._asdict()) for t in terms]


@app.post("/api/glossary", response_model=TermOut, status_code=201)
async def create_glossary_term(payload: TermIn):
    """Create a vocabulary override."""
    try:
        term = glossary.create_term(
            db.get_connection(),
            payload.target_lang,
            payload.source_term,
            payload.target_term,
            payload.match_case,
            payload.enabled,
            payload.note,
        )
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except glossary.DuplicateTermError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info("Created glossary term: %s -> %s", term.source_term, term.target_term)
    return TermOut(**term._asdict())


# Declared before /api/glossary/{term_id}, or FastAPI matches "export" and
# "import" as a term_id and returns 422.
@app.get("/api/glossary/export")
async def export_glossary(target_lang: Optional[str] = None):
    """Download the vocabulary as CSV."""
    body = glossary.export_csv(db.get_connection(), target_lang)
    suffix = target_lang or "all"
    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="glossary_{suffix}.csv"'
        },
    )


@app.post("/api/glossary/import")
async def import_glossary(
    target_lang: str = "da",
    mode: str = "merge",
    file: UploadFile = File(...),
):
    """Upload a CSV vocabulary, merging into or replacing the current one."""
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422, detail="File must be UTF-8 encoded"
        ) from e
    try:
        result = glossary.import_csv(db.get_connection(), content, target_lang, mode)
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return JSONResponse(content=result)


@app.put("/api/glossary/{term_id}", response_model=TermOut)
async def update_glossary_term(term_id: int, payload: TermUpdate):
    """Update a vocabulary override."""
    fields = payload.model_dump(exclude_none=True)
    try:
        term = glossary.update_term(db.get_connection(), term_id, **fields)
    except glossary.TermNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except glossary.DuplicateTermError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return TermOut(**term._asdict())


@app.delete("/api/glossary/{term_id}", status_code=204)
async def delete_glossary_term(term_id: int):
    """Delete a vocabulary override."""
    try:
        glossary.delete_term(db.get_connection(), term_id)
    except glossary.TermNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    # Bind to 0.0.0.0 for Docker container accessibility
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)  # nosec B104
