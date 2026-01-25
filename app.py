import os
import re
import logging
import asyncio
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import defusedxml.ElementTree as DET
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from werkzeug.utils import secure_filename as werkzeug_secure_filename

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
LIBRETRANSLATE_URL = "http://libretranslate:5000/translate"
LIBRETRANSLATE_LANGUAGES_URL = "http://libretranslate:5000/languages"

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

# Global HTTP client
http_client: Optional[httpx.AsyncClient] = None


# Pydantic models
class UploadResponse(BaseModel):
    message: str
    download_url: str


class HealthCheckResponse(BaseModel):
    status: str
    libretranslate: str
    filesystem: str


# Lifespan management
@asynccontextmanager
async def lifespan(_app: FastAPI):  # pylint: disable=redefined-outer-name,unused-argument
    """Manage application lifespan for httpx client initialization and cleanup."""
    global http_client  # pylint: disable=global-statement
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
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
    """Ensures placeholders like %1$s and %n remain correctly formatted with a leading space if needed."""
    if text:
        text = re.sub(
            r"(?<!\s)%\s*(\d+)\s*\$", r" %\1$", text
        )  # Ensure space before %1$s
        text = re.sub(r"(?<!\s)%\s*n", r" %n", text)  # Ensure space before %n
    return text


# Translation functions
async def translate_text(text: str, target_lang: str = "da") -> str:
    """Translates text using LibreTranslate with retry logic."""
    if not text:
        return text

    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    max_retries = 3

    for attempt in range(max_retries):
        try:
            logger.debug(
                "Translation attempt %d/%d for text: %s...",
                attempt + 1, max_retries, text[:50]
            )
            response = await http_client.post(
                LIBRETRANSLATE_URL, json=payload, timeout=30.0
            )
            response.raise_for_status()
            translated = response.json().get("translatedText", text)
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


async def translate_xliff(
    input_file: str, output_file: str, target_lang: str = "da"
) -> str:
    """Parses an XLIFF file, translates text, and saves the translated file in the correct Transifex format."""
    try:
        logger.info("Starting XLIFF translation: %s -> %s", input_file, output_file)
        tree = DET.parse(input_file)  # Securely parse XML
        root = tree.getroot()

        trans_units = root.findall(".//trans-unit")
        logger.info("Found %d translation units", len(trans_units))

        for idx, trans_unit in enumerate(trans_units):
            source = trans_unit.find("source")
            target = trans_unit.find("target")

            if source is not None and source.text:
                logger.debug("Translating unit %d/%d", idx + 1, len(trans_units))
                translated_text = await translate_text(source.text, target_lang)
                translated_text = fix_placeholder_formatting(translated_text)

                if target is None:
                    target = ET.SubElement(
                        trans_unit, "target"
                    )  # Ensure Transifex compatibility
                target.text = translated_text
                target.set(
                    "state", "needs-review-translation"
                )  # Set state for Transifex validation

        # Convert to standard ElementTree for writing the XML safely
        new_tree = ET.ElementTree(root)
        new_tree.write(output_file, encoding="utf-8", xml_declaration=True)
        logger.info("XLIFF translation completed: %s", output_file)

        return output_file
    except HTTPException:
        # Re-raise HTTPExceptions (like 504 timeout) without wrapping
        raise
    except Exception as e:
        logger.error("Error during XLIFF translation: %s", e)
        raise HTTPException(
            status_code=500, detail=f"XLIFF processing failed: {str(e)}"
        ) from e


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
    """Handle XLIFF file upload and translation."""
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
    file_path = os.path.join(UPLOAD_FOLDER, filename)

    # Validate path to prevent directory traversal
    if not validate_path_in_directory(file_path, UPLOAD_FOLDER):
        logger.error("Path traversal attempt detected: %s", file_path)
        raise HTTPException(status_code=400, detail="Invalid file path")

    try:
        # Save uploaded file
        logger.info("Saving uploaded file: %s", filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Translate
        translated_filename = secure_filename(f"translated_{filename}")
        output_file = os.path.join(PROCESSED_FOLDER, translated_filename)

        # Validate output path to prevent directory traversal
        if not validate_path_in_directory(output_file, PROCESSED_FOLDER):
            logger.error("Path traversal attempt in output path: %s", output_file)
            raise HTTPException(status_code=400, detail="Invalid output path")

        await translate_xliff(file_path, output_file)

        logger.info("File processed successfully: %s", translated_filename)
        return UploadResponse(
            message="File processed successfully",
            download_url=f"/download/{translated_filename}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing upload: %s", e)
        raise HTTPException(
            status_code=500, detail=f"File processing failed: {str(e)}"
        ) from e


@app.get("/download/{filename}")
async def download_file(filename: str):
    """Download translated XLIFF file."""
    safe_filename = secure_filename(filename)
    file_path = os.path.join(PROCESSED_FOLDER, safe_filename)

    # Validate path to prevent directory traversal
    if not validate_path_in_directory(file_path, PROCESSED_FOLDER):
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
        response = await http_client.get(LIBRETRANSLATE_LANGUAGES_URL, timeout=5.0)
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
        test_file_uploads = os.path.join(UPLOAD_FOLDER, ".health_check")
        with open(test_file_uploads, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file_uploads)

        # Test write to processed
        test_file_processed = os.path.join(PROCESSED_FOLDER, ".health_check")
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
        status, libretranslate_status, filesystem_status
    )
    return HealthCheckResponse(
        status=status,
        libretranslate=libretranslate_status,
        filesystem=filesystem_status,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5003)
