"""Tests for the retention cleanup module (files + in-memory job store)."""
import asyncio
import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import cleanup
import translation
from app import app
from settings import settings
from translation import jobs

DAY = 86400


def _write(path, age_seconds=0.0):
    """Create a file and back-date its mtime by age_seconds."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """Point the upload/processed settings at empty temporary directories."""
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()
    monkeypatch.setattr(settings, "upload_folder", str(uploads))
    monkeypatch.setattr(settings, "processed_folder", str(processed))
    return uploads, processed


@pytest.fixture
def empty_jobs():
    """Run each job test against an empty job store and leave it empty."""
    jobs.clear()
    yield jobs
    jobs.clear()


class TestFileCleanup:
    """Aged files are pruned, recent files survive, odd cases do not explode."""

    def test_removes_files_older_than_retention(self, folders):
        uploads, processed = folders
        old_upload = _write(uploads / "old.xlf", age_seconds=8 * DAY)
        old_processed = _write(processed / "translated_old.xlf", age_seconds=30 * DAY)

        removed = cleanup.cleanup_files(retention_days=7)

        assert removed == 2
        assert not os.path.exists(old_upload)
        assert not os.path.exists(old_processed)

    def test_keeps_files_inside_retention_window(self, folders):
        uploads, processed = folders
        fresh = _write(uploads / "fresh.xlf")
        recent = _write(processed / "recent.xlf", age_seconds=6 * DAY)

        removed = cleanup.cleanup_files(retention_days=7)

        assert removed == 0
        assert os.path.exists(fresh)
        assert os.path.exists(recent)

    def test_retention_days_defaults_to_settings(self, folders, monkeypatch):
        uploads, _ = folders
        monkeypatch.setattr(settings, "file_retention_days", 1)
        old = _write(uploads / "two-days.xlf", age_seconds=2 * DAY)

        assert cleanup.cleanup_files() == 1
        assert not os.path.exists(old)

    def test_missing_directory_is_tolerated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_folder", str(tmp_path / "nope"))
        monkeypatch.setattr(settings, "processed_folder", str(tmp_path / "also-nope"))

        assert cleanup.cleanup_files(retention_days=7) == 0

    def test_subdirectories_are_left_alone(self, folders):
        uploads, _ = folders
        nested = uploads / "nested"
        nested.mkdir()
        stamp = time.time() - 30 * DAY
        os.utime(nested, (stamp, stamp))

        assert cleanup.cleanup_files(retention_days=7) == 0
        assert nested.is_dir()

    def test_undeletable_file_is_logged_and_sweep_continues(self, folders, caplog):
        uploads, _ = folders
        stubborn = str(_write(uploads / "locked.xlf", age_seconds=9 * DAY))
        deletable = str(_write(uploads / "deletable.xlf", age_seconds=9 * DAY))
        real_remove = os.remove

        def fake_remove(path):
            if os.path.abspath(path) == os.path.abspath(stubborn):
                raise PermissionError("Operation not permitted")
            real_remove(path)

        with caplog.at_level(logging.WARNING, logger="cleanup"):
            with patch("cleanup.os.remove", side_effect=fake_remove):
                removed = cleanup.cleanup_files(retention_days=7)

        assert removed == 1
        assert os.path.exists(stubborn)
        assert not os.path.exists(deletable)
        assert "locked.xlf" in caplog.text

    def test_unreadable_directory_is_logged_and_tolerated(self, folders, caplog):
        with caplog.at_level(logging.WARNING, logger="cleanup"):
            with patch("cleanup.os.scandir", side_effect=OSError("boom")):
                assert cleanup.cleanup_files(retention_days=7) == 0

        assert "boom" in caplog.text


class TestJobCleanup:
    """Finished job entries are evicted, but not before the grace period."""

    def test_finished_job_survives_the_grace_period(self, empty_jobs):
        empty_jobs["fresh"] = {
            "status": "completed",
            "completed": 5,
            "total": 5,
            "download_url": "/download/translated_test.xlf",
            "error": None,
            "task": None,
            "finished_at": time.time() - 10,
        }

        assert cleanup.cleanup_jobs(grace_seconds=300) == 0
        assert "fresh" in empty_jobs

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_finished_job_is_evicted_after_the_grace_period(self, empty_jobs, status):
        empty_jobs["stale"] = {
            "status": status,
            "completed": 1,
            "total": 1,
            "download_url": None,
            "error": None,
            "task": None,
            "finished_at": time.time() - 600,
        }

        assert cleanup.cleanup_jobs(grace_seconds=300) == 1
        assert "stale" not in empty_jobs

    def test_finished_job_without_timestamp_is_stamped_then_evicted(self, empty_jobs):
        empty_jobs["undated"] = {
            "status": "completed",
            "completed": 1,
            "total": 1,
            "download_url": None,
            "error": None,
            "task": None,
        }

        # First pass only stamps it, so the grace period starts from a known point.
        assert cleanup.cleanup_jobs(grace_seconds=300) == 0
        assert empty_jobs["undated"]["finished_at"] == pytest.approx(time.time(), abs=5)

        empty_jobs["undated"]["finished_at"] -= 600
        assert cleanup.cleanup_jobs(grace_seconds=300) == 1
        assert "undated" not in empty_jobs

    def test_running_job_is_never_evicted(self, empty_jobs):
        running_task = MagicMock()
        running_task.done.return_value = False
        empty_jobs["running"] = {
            "status": "running",
            "completed": 3,
            "total": 10,
            "download_url": None,
            "error": None,
            "task": running_task,
            "first_seen": time.time() - 30 * DAY,
        }

        assert cleanup.cleanup_jobs(retention_days=7, grace_seconds=0) == 0
        assert "running" in empty_jobs

    def test_abandoned_job_is_evicted_after_the_retention_window(self, empty_jobs):
        empty_jobs["abandoned"] = {
            "status": "pending",
            "completed": 0,
            "total": 0,
            "download_url": None,
            "error": None,
            "task": None,
            "first_seen": time.time() - 8 * DAY,
        }

        assert cleanup.cleanup_jobs(retention_days=7, grace_seconds=300) == 1
        assert "abandoned" not in empty_jobs

    def test_unfinished_job_is_kept_inside_the_retention_window(self, empty_jobs):
        empty_jobs["pending"] = {
            "status": "pending",
            "completed": 0,
            "total": 0,
            "download_url": None,
            "error": None,
            "task": None,
        }

        assert cleanup.cleanup_jobs(retention_days=7, grace_seconds=300) == 0
        assert empty_jobs["pending"]["first_seen"] == pytest.approx(time.time(), abs=5)

    def test_grace_period_defaults_to_settings(self, empty_jobs, monkeypatch):
        monkeypatch.setattr(settings, "job_grace_period_minutes", 1)
        empty_jobs["stale"] = {
            "status": "completed",
            "completed": 1,
            "total": 1,
            "download_url": None,
            "error": None,
            "task": None,
            "finished_at": time.time() - 120,
        }

        assert cleanup.cleanup_jobs() == 1

    def test_progress_still_answers_for_a_client_polling_after_completion(self, empty_jobs):
        """A just-finished job stays queryable through /progress during the grace period."""
        empty_jobs["polling"] = {
            "status": "completed",
            "completed": 2,
            "total": 2,
            "download_url": "/download/translated_test.xlf",
            "error": None,
            "task": None,
            "finished_at": time.time(),
        }

        cleanup.cleanup_jobs(grace_seconds=300)
        response = TestClient(app).get("/progress/polling")

        assert response.status_code == 200
        assert response.json()["download_url"] == "/download/translated_test.xlf"


class TestFinishJobTimestamps:
    """The timestamp cleanup evicts on is written when a job reaches a terminal state."""

    def test_terminal_status_is_stamped(self, empty_jobs):
        empty_jobs["job"] = {"status": "running", "completed": 1, "total": 2,
                             "download_url": None, "error": None, "task": None}

        translation.finish_job("job", "completed")

        assert empty_jobs["job"]["status"] == "completed"
        assert empty_jobs["job"]["finished_at"] == pytest.approx(time.time(), abs=5)

    def test_unknown_job_is_a_no_op(self, empty_jobs):
        translation.finish_job("gone", "completed")

        assert "gone" not in empty_jobs


class TestRunCleanup:
    """A pass reports what it removed."""

    def test_reports_file_and_job_counts(self, folders, empty_jobs):
        uploads, _ = folders
        _write(uploads / "old.xlf", age_seconds=30 * DAY)
        empty_jobs["stale"] = {
            "status": "failed",
            "completed": 0,
            "total": 1,
            "download_url": None,
            "error": "nope",
            "task": None,
            "finished_at": time.time() - 30 * DAY,
        }

        assert cleanup.run_cleanup() == (1, 1)

    def test_logs_what_was_removed(self, folders, empty_jobs, caplog):
        uploads, _ = folders
        _write(uploads / "old.xlf", age_seconds=30 * DAY)

        with caplog.at_level(logging.INFO, logger="cleanup"):
            cleanup.run_cleanup()

        assert "old.xlf" in caplog.text
        assert "1 file" in caplog.text


class TestPeriodicTask:
    """The periodic task starts, repeats, survives failures, and stops on demand."""

    @pytest.fixture(autouse=True)
    async def _no_leaked_task(self):
        yield
        await cleanup.stop_cleanup_task()

    async def test_start_runs_one_pass_immediately(self, folders, empty_jobs):
        with patch("cleanup.run_cleanup", return_value=(0, 0)) as pass_mock:
            await cleanup.start_cleanup_task(interval_seconds=3600)
            assert pass_mock.call_count == 1

        assert cleanup.cleanup_task is not None
        assert not cleanup.cleanup_task.done()

    async def test_task_runs_repeatedly(self, folders, empty_jobs):
        with patch("cleanup.run_cleanup", return_value=(0, 0)) as pass_mock:
            await cleanup.start_cleanup_task(interval_seconds=0.01)
            await asyncio.sleep(0.08)
            calls_while_running = pass_mock.call_count

            await cleanup.stop_cleanup_task()
            await asyncio.sleep(0.05)

            assert calls_while_running >= 2
            assert pass_mock.call_count == calls_while_running

    async def test_task_really_deletes_an_expired_file_on_a_tick(self, folders, empty_jobs, monkeypatch):
        uploads, _ = folders
        monkeypatch.setattr(settings, "file_retention_days", 7)
        # Written after the startup pass would have run, so only a periodic tick
        # can remove it.
        with patch("cleanup.run_cleanup", return_value=(0, 0)):
            await cleanup.start_cleanup_task(interval_seconds=0.01)
        expired = _write(uploads / "expired.xlf", age_seconds=30 * DAY)

        for _ in range(100):
            await asyncio.sleep(0.01)
            if not os.path.exists(expired):
                break

        assert not os.path.exists(expired)

    async def test_stop_cancels_the_task(self, folders, empty_jobs):
        await cleanup.start_cleanup_task(interval_seconds=3600)
        task = cleanup.cleanup_task

        await cleanup.stop_cleanup_task()

        assert task.cancelled()
        assert cleanup.cleanup_task is None

    async def test_stop_without_start_is_a_no_op(self):
        await cleanup.stop_cleanup_task()
        assert cleanup.cleanup_task is None

    async def test_failing_pass_does_not_kill_the_task(self, folders, empty_jobs, caplog):
        with caplog.at_level(logging.ERROR, logger="cleanup"):
            with patch("cleanup.run_cleanup", side_effect=RuntimeError("disk gone")) as pass_mock:
                await cleanup.start_cleanup_task(interval_seconds=0.01)
                await asyncio.sleep(0.05)

                assert pass_mock.call_count >= 2
                assert not cleanup.cleanup_task.done()

        assert "disk gone" in caplog.text


class TestLifespanIntegration:
    """The task is owned by the FastAPI lifespan: started on entry, cancelled on exit."""

    async def test_lifespan_starts_and_stops_the_cleanup_task(self, folders, empty_jobs):
        with patch("translation.startup_http_client", new=AsyncMock()), patch(
            "translation.shutdown_http_client", new=AsyncMock()
        ), patch("db.startup_database"), patch("db.shutdown_database"), patch(
            "cleanup.run_cleanup", return_value=(0, 0)
        ) as pass_mock:
            async with app.router.lifespan_context(app):
                task = cleanup.cleanup_task
                assert task is not None
                assert not task.done()
                assert pass_mock.call_count == 1

            assert task.cancelled()
            assert cleanup.cleanup_task is None
