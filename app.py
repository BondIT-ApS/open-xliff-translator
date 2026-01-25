import os
import re
import logging
import asyncio
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import defusedxml.ElementTree as DET
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
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
    lifespan=lifespan
)

templates = Jinja2Templates(directory="templates")

# Utility functions
def secure_filename(filename: str) -> str:
    """
    Sanitize filename to prevent directory traversal attacks.
    Replacement for werkzeug.utils.secure_filename.
    """
    # Remove any path components
    filename = os.path.basename(filename)
    # Remove any non-alphanumeric characters except dots, dashes, and underscores
    filename = re.sub(r'[^\w\s.-]', '', filename)
    # Replace spaces with underscores
    filename = filename.replace(' ', '_')
    # Remove leading dots to prevent hidden files
    filename = filename.lstrip('.')
    return filename or "unnamed"

def fix_placeholder_formatting(text: str) -> str:
    """Ensures placeholders like %1$s and %n remain correctly formatted with a leading space if needed."""
    if text:
        text = re.sub(r"(?<!\s)%\s*(\d+)\s*\$", r" %\1$", text)  # Ensure space before %1$s
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
            logger.debug(f"Translation attempt {attempt + 1}/{max_retries} for text: {text[:50]}...")
            response = await http_client.post(LIBRETRANSLATE_URL, json=payload, timeout=30.0)
            response.raise_for_status()
            translated = response.json().get("translatedText", text)
            logger.debug(f"Translation successful: {translated[:50]}...")
            return translated
        except httpx.TimeoutException as e:
            logger.warning(f"LibreTranslate timeout on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                logger.error("LibreTranslate timeout after all retry attempts")
                raise HTTPException(status_code=504, detail="Translation service timeout")
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
        except httpx.HTTPStatusError as e:
            logger.error(f"LibreTranslate HTTP error on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise HTTPException(status_code=502, detail=f"Translation service error: {e.response.status_code}")
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Unexpected error during translation: {e}")
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Translation failed")
            await asyncio.sleep(2 ** attempt)

    return text  # Fallback

async def translate_xliff(input_file: str, output_file: str, target_lang: str = "da") -> str:
    """Parses an XLIFF file, translates text, and saves the translated file in the correct Transifex format."""
    try:
        logger.info(f"Starting XLIFF translation: {input_file} -> {output_file}")
        tree = DET.parse(input_file)  # Securely parse XML
        root = tree.getroot()

        trans_units = root.findall(".//trans-unit")
        logger.info(f"Found {len(trans_units)} translation units")

        for idx, trans_unit in enumerate(trans_units):
            source = trans_unit.find("source")
            target = trans_unit.find("target")

            if source is not None and source.text:
                logger.debug(f"Translating unit {idx + 1}/{len(trans_units)}")
                translated_text = await translate_text(source.text, target_lang)
                translated_text = fix_placeholder_formatting(translated_text)

                if target is None:
                    target = ET.SubElement(trans_unit, "target")  # Ensure Transifex compatibility
                target.text = translated_text
                target.set("state", "needs-review-translation")  # Set state for Transifex validation

        # Convert to standard ElementTree for writing the XML safely
        new_tree = ET.ElementTree(root)
        new_tree.write(output_file, encoding="utf-8", xml_declaration=True)
        logger.info(f"XLIFF translation completed: {output_file}")

        return output_file
    except Exception as e:
        logger.error(f"Error during XLIFF translation: {e}")
        raise HTTPException(status_code=500, detail=f"XLIFF processing failed: {str(e)}")

# Routes
@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main upload interface."""
    try:
        with open("templates/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        logger.error("index.html template not found")
        raise HTTPException(status_code=500, detail="Template not found")

@app.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)):
    """Handle XLIFF file upload and translation."""
    if not file:
        logger.warning("Upload request with no file")
        raise HTTPException(status_code=400, detail="No file part")

    if not file.filename:
        logger.warning("Upload request with empty filename")
        raise HTTPException(status_code=400, detail="No selected file")

    if not file.filename.endswith('.xlf'):
        logger.warning(f"Invalid file extension: {file.filename}")
        raise HTTPException(status_code=400, detail="Only .xlf files are allowed")

    filename = secure_filename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)

    try:
        # Save uploaded file
        logger.info(f"Saving uploaded file: {filename}")
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Translate
        translated_filename = secure_filename(f"translated_{filename}")
        output_file = os.path.join(PROCESSED_FOLDER, translated_filename)
        await translate_xliff(file_path, output_file)

        logger.info(f"File processed successfully: {translated_filename}")
        return UploadResponse(
            message="File processed successfully",
            download_url=f"/download/{translated_filename}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing upload: {e}")
        raise HTTPException(status_code=500, detail=f"File processing failed: {str(e)}")

@app.get("/download/{filename}")
async def download_file(filename: str):
    """Download translated XLIFF file."""
    safe_filename = secure_filename(filename)
    file_path = os.path.join(PROCESSED_FOLDER, safe_filename)

    # Ensure the file exists before attempting to send it
    if not os.path.exists(file_path):
        logger.warning(f"Download requested for non-existent file: {safe_filename}")
        raise HTTPException(status_code=404, detail="File not found")

    logger.info(f"Serving file for download: {safe_filename}")
    return FileResponse(
        path=file_path,
        filename=safe_filename,
        media_type="application/xml"
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
            logger.warning(f"LibreTranslate health check failed: {response.status_code}")
    except Exception as e:
        status = "degraded"
        logger.warning(f"LibreTranslate health check exception: {e}")

    # Check filesystem
    try:
        # Test write to uploads
        test_file_uploads = os.path.join(UPLOAD_FOLDER, ".health_check")
        with open(test_file_uploads, "w") as f:
            f.write("ok")
        os.remove(test_file_uploads)

        # Test write to processed
        test_file_processed = os.path.join(PROCESSED_FOLDER, ".health_check")
        with open(test_file_processed, "w") as f:
            f.write("ok")
        os.remove(test_file_processed)

        filesystem_status = "writable"
        logger.debug("Filesystem health check passed")
    except Exception as e:
        status = "degraded"
        logger.warning(f"Filesystem health check failed: {e}")

    # Determine overall status
    if libretranslate_status == "unavailable" and filesystem_status == "readonly":
        status = "unhealthy"
        logger.error("Health check failed: both LibreTranslate and filesystem unavailable")
        raise HTTPException(status_code=503, detail="Service unhealthy")

    logger.info(f"Health check: {status} (LibreTranslate: {libretranslate_status}, Filesystem: {filesystem_status})")
    return HealthCheckResponse(
        status=status,
        libretranslate=libretranslate_status,
        filesystem=filesystem_status
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5003)
