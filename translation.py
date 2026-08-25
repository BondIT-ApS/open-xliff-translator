"""Translation pipeline: placeholder masking, LibreTranslate calls, XLIFF processing."""
import os
import re
import html
import time
import logging
import asyncio
import xml.etree.ElementTree as ET  # nosec B405 - Only used for writing XML, not parsing
from typing import Any, Dict, NamedTuple, Optional

import httpx
import defusedxml.ElementTree as DET
from fastapi import HTTPException

import db
import glossary
from settings import settings

logger = logging.getLogger(__name__)

# Global HTTP client, initialised by startup_http_client during app lifespan.
# Referenced by name (not from-imported) so tests can patch translation.http_client.
http_client: Optional[httpx.AsyncClient] = None

# In-memory job store: job_id -> job state dict
jobs: Dict[str, Dict[str, Any]] = {}


def finish_job(job_id: str, status: str) -> None:
    """Record a terminal job status plus when it was reached, so cleanup can evict it."""
    job = jobs.get(job_id)
    if job is None:
        return
    job["status"] = status
    job["finished_at"] = time.time()


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


class TranslationResult(NamedTuple):
    """The translated text plus how many glossary overrides were applied."""

    text: str
    terms_applied: int


# Translation functions
async def translate_text(
    text: str, target_lang: Optional[str] = None
) -> TranslationResult:
    """Translate text with retry logic, applying vocabulary overrides."""
    if not text:
        return TranslationResult(text, 0)

    if target_lang is None:
        target_lang = settings.default_target_language

    # Placeholders are masked FIRST so a glossary term can never match inside
    # one; both share the same sentinel index space.
    masked_text, originals = mask_placeholders(text)

    terms_applied = 0
    if settings.glossary_enabled:
        try:
            compiled = glossary.get_compiled(db.get_connection(), target_lang)
            masked_text, terms_applied = glossary.mask_glossary(
                masked_text, compiled, originals
            )
        except Exception as e:  # pylint: disable=broad-except
            # A broken glossary degrades quality; it must never fail the job.
            logger.warning("Glossary unavailable, translating without overrides: %s", e)

    # Nothing meaningful left for the engine. Restore rather than returning the
    # raw source, or every override in this segment would be silently dropped.
    if not has_translatable_text(masked_text):
        return TranslationResult(
            restore_placeholders(masked_text, originals), terms_applied
        )

    payload = {
        "q": masked_text,
        "source": "auto",
        "target": target_lang,
        "format": "html",
    }
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
            translated = restore_placeholders(translated, originals)
            logger.debug("Translation successful: %s...", translated[:50])
            return TranslationResult(translated, terms_applied)
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
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Unexpected error during translation: %s", e)
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Translation failed") from e
            await asyncio.sleep(2**attempt)

    # Fallback: restore rather than returning raw source, so overrides survive.
    return TranslationResult(
        restore_placeholders(masked_text, originals), terms_applied
    )


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
        jobs[job_id]["terms_applied"] = 0
        logger.info("Job %s: found %d translation units", job_id, len(trans_units))

        for idx, trans_unit in enumerate(trans_units):
            if jobs[job_id]["status"] in ("cancelled", "cancelling"):
                finish_job(job_id, "cancelled")
                logger.info("Job %s: cancelled at unit %d/%d", job_id, idx + 1, len(trans_units))
                return

            source = trans_unit.find("source")
            target = trans_unit.find("target")

            if source is not None and source.text:
                result = await translate_text(source.text, target_lang)
                translated_text = result.text
                jobs[job_id]["terms_applied"] += result.terms_applied

                if target is None:
                    target = ET.SubElement(trans_unit, "target")  # Ensure Transifex compatibility
                target.text = translated_text
                target.set("state", "needs-review-translation")  # Set state for Transifex validation

            jobs[job_id]["completed"] = idx + 1

        new_tree = ET.ElementTree(root)
        new_tree.write(output_file, encoding="utf-8", xml_declaration=True)
        finish_job(job_id, "completed")
        jobs[job_id]["download_url"] = f"/download/{os.path.basename(output_file)}"
        logger.info("Job %s: completed successfully", job_id)

    except asyncio.CancelledError:
        finish_job(job_id, "cancelled")
        logger.info("Job %s: was cancelled", job_id)
        raise
    except HTTPException as e:
        finish_job(job_id, "failed")
        jobs[job_id]["error"] = e.detail
        logger.error("Job %s: failed with HTTP error: %s", job_id, e.detail)
    except Exception as e:
        finish_job(job_id, "failed")
        jobs[job_id]["error"] = f"XLIFF processing failed: {str(e)}"
        logger.error("Job %s: failed with unexpected error: %s", job_id, e)
