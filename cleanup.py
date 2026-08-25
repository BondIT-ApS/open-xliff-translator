"""Retention cleanup: prunes aged upload/processed files and finished job entries.

Nothing in the app removes what it creates, so both the two file folders and the
in-memory job store grow for the lifetime of the container. This module sweeps
both on a periodic asyncio task owned by the FastAPI lifespan — no scheduler
dependency and no extra container.
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from settings import settings
from translation import jobs

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400

# Job states that will never change again, and are therefore evictable once the
# grace period below has passed.
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")

# Handle for the periodic task, created by start_cleanup_task during the app
# lifespan. Referenced by name (not from-imported) so tests can inspect it.
cleanup_task: Optional[asyncio.Task] = None


def _retention_cutoff(retention_days: Optional[float]) -> float:
    """Return the timestamp before which content is considered expired."""
    if retention_days is None:
        retention_days = settings.file_retention_days
    return time.time() - retention_days * SECONDS_PER_DAY


def _cleanup_folder(folder: str, cutoff: float) -> int:
    """Delete files in one folder last modified before cutoff. Returns the count."""
    if not os.path.isdir(folder):
        logger.debug("Cleanup: folder %s does not exist, nothing to prune", folder)
        return 0

    try:
        entries = list(os.scandir(folder))
    except OSError as e:
        logger.warning("Cleanup: could not list folder %s: %s", folder, e)
        return 0

    removed = 0
    for entry in entries:
        try:
            # Only plain files are ours to remove; subdirectories are left alone.
            if not entry.is_file():
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            os.remove(entry.path)
            removed += 1
            logger.info("Cleanup: removed expired file %s", entry.path)
        except OSError as e:
            # One undeletable file (permissions, in use, a race with a download)
            # must never abort the sweep or kill the periodic task.
            logger.warning("Cleanup: could not remove %s: %s", entry.path, e)
    return removed


def cleanup_files(retention_days: Optional[float] = None) -> int:
    """Delete upload/processed files older than the retention window."""
    cutoff = _retention_cutoff(retention_days)
    return sum(
        _cleanup_folder(folder, cutoff)
        for folder in (settings.upload_folder, settings.processed_folder)
    )


def _is_evictable(
    job_id: str, job: Dict[str, Any], now: float, grace_seconds: float, cutoff: float
) -> bool:
    """Decide whether a single job entry can be dropped from the store."""
    status = job.get("status")

    if status in TERMINAL_JOB_STATUSES:
        finished_at = job.get("finished_at")
        if finished_at is None:
            # Terminal but unstamped (finished outside the translation pipeline,
            # or already in the store before this build): stamp it now so the
            # grace period is measured from a point we actually know.
            job["finished_at"] = now
            return False
        if now - finished_at < grace_seconds:
            # A client may still be polling /progress/{job_id}; answering beats
            # a 404 for the few minutes after the job finishes.
            return False
        logger.info(
            "Cleanup: evicting %s job %s (finished %.0fs ago)", status, job_id, now - finished_at
        )
        return True

    task = job.get("task")
    if task is not None and not task.done():
        # Still translating. Dropping the entry would pull the store out from
        # under the running coroutine, which writes progress into it.
        return False

    # Unfinished and going nowhere: evict once it is past the retention window.
    started_at = job.get("created_at")
    if started_at is None:
        started_at = job.setdefault("first_seen", now)
    if started_at > cutoff:
        return False

    logger.warning("Cleanup: evicting abandoned %s job %s", status, job_id)
    return True


def cleanup_jobs(
    retention_days: Optional[float] = None, grace_seconds: Optional[float] = None
) -> int:
    """Evict finished job entries past the grace period and abandoned ones past retention."""
    if grace_seconds is None:
        grace_seconds = settings.job_grace_period_minutes * 60
    cutoff = _retention_cutoff(retention_days)
    now = time.time()

    removed = 0
    for job_id, job in list(jobs.items()):
        if _is_evictable(job_id, job, now, grace_seconds, cutoff):
            jobs.pop(job_id, None)
            removed += 1
    return removed


def run_cleanup() -> Tuple[int, int]:
    """Run one full pass over files and jobs. Returns (files removed, jobs evicted)."""
    files_removed = cleanup_files()
    jobs_evicted = cleanup_jobs()
    logger.info(
        "Cleanup pass complete: %d file(s) removed, %d job entries evicted "
        "(retention %s day(s), %d job(s) still tracked)",
        files_removed,
        jobs_evicted,
        settings.file_retention_days,
        len(jobs),
    )
    return files_removed, jobs_evicted


def _run_cleanup_guarded() -> None:
    """Run a pass, absorbing anything unexpected so the periodic task stays alive."""
    try:
        run_cleanup()
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Cleanup pass failed: %s", e)


async def _cleanup_loop(interval_seconds: float) -> None:
    """Sleep-and-sweep forever until the lifespan cancels us."""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            _run_cleanup_guarded()
    except asyncio.CancelledError:
        logger.info("Cleanup task cancelled")
        raise


async def start_cleanup_task(interval_seconds: Optional[float] = None) -> None:
    """Sweep once, then start the periodic task. Called from the FastAPI lifespan."""
    global cleanup_task  # pylint: disable=global-statement
    if interval_seconds is None:
        interval_seconds = settings.cleanup_interval_hours * 3600

    _run_cleanup_guarded()
    cleanup_task = asyncio.create_task(_cleanup_loop(interval_seconds))
    logger.info(
        "Cleanup task started (every %.0fs, retention %s day(s))",
        interval_seconds,
        settings.file_retention_days,
    )


async def stop_cleanup_task() -> None:
    """Cancel the periodic task and wait for it to finish. Called from the lifespan."""
    global cleanup_task  # pylint: disable=global-statement
    if cleanup_task is None:
        return

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    cleanup_task = None
    logger.info("Cleanup task stopped")
