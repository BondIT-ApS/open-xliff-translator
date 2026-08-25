"""
Session-scoped download history (issue #28).

Identity is an anonymous session cookie: an opaque random id scoped to the
browser, with no accounts and no login. It selects *which* history to show and
nothing more — it is not an authentication credential, so nothing more
sensitive than a list of files this instance already stores may ever be gated
on it.

That ambient authority is kept harmless by the surface being read-only: list
and re-download, no delete, no mutation driven by the caller. A forged
cross-site request therefore has nothing to act on, and the response cannot be
read cross-origin. If a delete-history endpoint is ever added, real CSRF
protection becomes mandatory (see the decision recorded on issue #28).

Rows are written by the server from its own state, never from request bodies:
the job store in translation.py is the single source of truth for filenames,
sizes and outcome. Expiry is derived at read time from ``file_retention_days``
rather than stored, so changing the retention setting takes effect immediately
instead of leaving stale expiry stamps behind.
"""
import os
import re
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Cookie carrying the anonymous session id.
SESSION_COOKIE_NAME = "xliff_history_session"

# Stored row states.
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"  # job lost before it finished (e.g. a restart)

# Derived states, computed per request for completed rows only.
VIEW_AVAILABLE = "available"
VIEW_EXPIRED = "expired"
VIEW_UNAVAILABLE = "unavailable"

# The translated file is written as "translated_<original>" by the upload route.
TRANSLATED_PREFIX = "translated_"

# Newly generated ids are URL-safe base64; the bound is generous enough to keep
# accepting ids issued by earlier releases while rejecting obvious junk.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

# How many rows a session's history shows.
DEFAULT_LIMIT = 25


def new_session_id() -> str:
    """Return a fresh, unguessable session id."""
    return secrets.token_urlsafe(32)


def is_valid_session_id(value: Optional[str]) -> bool:
    """Report whether a cookie value has the shape of an id we issued."""
    return bool(value) and _SESSION_ID_PATTERN.match(value) is not None


def cookie_max_age(retention_days: int) -> int:
    """Cookie lifetime, tied to how long the files it can reveal survive."""
    return max(int(retention_days), 1) * 24 * 60 * 60


def set_session_cookie(response: Any, session_id: str, *, secure: bool, retention_days: int) -> None:
    """
    Attach the session cookie to a response.

    HttpOnly keeps it out of reach of page scripts, SameSite=Lax defeats the
    realistic cross-site cases, and Secure is set whenever the request arrived
    over HTTPS.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=cookie_max_age(retention_days),
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_job(
    conn: sqlite3.Connection,
    session_id: str,
    job_id: str,
    target_language: str,
    source_language: str = "auto",
) -> None:
    """
    Record a started translation against a session.

    Written as soon as the job exists so history survives the browser being
    closed mid-translation. Filenames and size are filled in by reconcile()
    once the job store reports an outcome. The insert is ignored if the job is
    already recorded, so a session can never claim another session's job.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO translations
            (session_id, job_id, created_at, source_language, target_language, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, job_id, _now_iso(), source_language, target_language, STATUS_PROCESSING),
    )
    conn.commit()


def reconcile(
    conn: sqlite3.Connection,
    jobs: Dict[str, Dict[str, Any]],
    processed_folder: str,
) -> int:
    """
    Persist the outcome of in-flight jobs from the in-memory job store.

    Called while the UI polls progress and when history is listed, so a
    finished job's filename is durable well before the browser is closed. A
    row whose job the store no longer knows about (the process restarted
    mid-translation) is marked interrupted rather than left pending forever.
    Returns the number of rows updated.
    """
    pending = conn.execute(
        "SELECT id, job_id FROM translations WHERE status = ?", (STATUS_PROCESSING,)
    ).fetchall()
    updated = 0

    for row in pending:
        job = jobs.get(row["job_id"])
        if job is None:
            conn.execute(
                "UPDATE translations SET status = ?, completed_at = ? WHERE id = ?",
                (STATUS_INTERRUPTED, _now_iso(), row["id"]),
            )
            updated += 1
            continue

        status = job.get("status")
        if status == "completed":
            translated = os.path.basename(job.get("download_url") or "")
            original = (
                translated[len(TRANSLATED_PREFIX):]
                if translated.startswith(TRANSLATED_PREFIX)
                else translated
            )
            file_path = os.path.join(processed_folder, translated)
            size = os.path.getsize(file_path) if translated and os.path.exists(file_path) else None
            conn.execute(
                """
                UPDATE translations
                   SET status = ?, completed_at = ?, translated_filename = ?,
                       original_filename = ?, file_size = ?
                 WHERE id = ?
                """,
                (STATUS_COMPLETED, _now_iso(), translated, original, size, row["id"]),
            )
            updated += 1
        elif status in ("failed", "cancelled"):
            conn.execute(
                "UPDATE translations SET status = ?, completed_at = ? WHERE id = ?",
                (STATUS_FAILED if status == "failed" else STATUS_CANCELLED, _now_iso(), row["id"]),
            )
            updated += 1

    if updated:
        conn.commit()
        logger.debug("History reconciled %d row(s)", updated)
    return updated


def _entry(
    row: sqlite3.Row,
    retention_days: int,
    resolve_path: Callable[[str], Optional[str]],
    now: datetime,
) -> Dict[str, Any]:
    """Build the API view of a stored row, deriving expiry and availability."""
    created = datetime.fromisoformat(row["created_at"])
    expires = created + timedelta(days=retention_days)
    translated = row["translated_filename"]

    status = row["status"]
    download_url = None
    if status == STATUS_COMPLETED:
        if now >= expires:
            status = VIEW_EXPIRED
        else:
            path = resolve_path(translated) if translated else None
            if path and os.path.exists(path):
                status = VIEW_AVAILABLE
                download_url = f"/download/{translated}"
            else:
                status = VIEW_UNAVAILABLE

    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "original_filename": row["original_filename"],
        "translated_filename": translated,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "expires_at": expires.isoformat(),
        "file_size": row["file_size"],
        "source_language": row["source_language"],
        "target_language": row["target_language"],
        "status": status,
        "download_url": download_url,
    }


def list_history(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    retention_days: int,
    resolve_path: Callable[[str], Optional[str]],
    limit: int = DEFAULT_LIMIT,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Return the newest rows belonging to one session, most recent first.

    Ownership is enforced in SQL, so a row of another session is never loaded,
    let alone filtered out later. ``resolve_path`` maps a stored filename to a
    validated path inside the processed folder (or None if it fails that
    validation) — the caller supplies it so history reuses the download
    route's own path handling instead of repeating it.
    """
    rows = conn.execute(
        """
        SELECT * FROM translations
         WHERE session_id = ?
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    moment = now or datetime.now(timezone.utc)
    return [_entry(row, retention_days, resolve_path, moment) for row in rows]
