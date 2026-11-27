"""
End-to-end tests for translate_xliff_with_progress (issue #176).

The translation engine is replaced by an echo that prefixes ``DA:``, so every
assertion is about how the XLIFF tree is read and written, not about
translation quality.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import defusedxml.ElementTree as DET

import translation
from settings import settings

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NS = {"x": XLIFF_NS}


async def _echo(_url, json=None, timeout=None):  # pylint: disable=redefined-outer-name,unused-argument
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"translatedText": "DA:" + json["q"]}
    return response


@pytest.fixture
def run_job(tmp_path, monkeypatch):
    """Run one translation job against an echo engine and return (job, output path)."""
    monkeypatch.setattr(settings, "glossary_enabled", False)
    client = MagicMock()
    client.post = AsyncMock(side_effect=_echo)

    async def _run(input_file):
        output_file = str(tmp_path / "out.xlf")
        job_id = "xliff-processing-test"
        translation.jobs[job_id] = {"status": "queued", "completed": 0, "total": 0}
        try:
            with patch.object(translation, "http_client", client):
                await translation.translate_xliff_with_progress(
                    job_id, input_file, output_file, "da"
                )
            return translation.jobs[job_id], output_file
        finally:
            translation.jobs.pop(job_id, None)

    return _run


class TestNamespacedXliff:
    """A file declaring the XLIFF 1.2 namespace is translated, not silently skipped."""

    async def test_every_unit_is_translated(self, run_job):
        job, output = await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))

        assert job["status"] == "completed"
        assert job["total"] == 5
        assert job["completed"] == 5

        root = DET.parse(output).getroot()
        targets = {
            unit.get("id"): unit.find("x:target", NS)
            for unit in root.iterfind(".//x:trans-unit", NS)
        }
        assert targets["1"].text == "DA:Hello World"
        # An existing target is overwritten, not duplicated beside a new one.
        assert targets["2"].text == "DA:You have %1$s messages"
        assert targets["3[0]"].text == "DA:One file"
        assert targets["3[1]"].text == "DA:%d files"
        # Placeholder-only: never sent to the engine, copied through.
        assert targets["4"].text == "%s"
        assert all(t.get("state") == "needs-review-translation" for t in targets.values())

    async def test_existing_target_is_not_duplicated(self, run_job):
        _, output = await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))

        root = DET.parse(output).getroot()
        for unit in root.iterfind(".//x:trans-unit", NS):
            assert len(unit.findall("x:target", NS)) == 1

    async def test_new_targets_are_in_the_xliff_namespace(self, run_job):
        _, output = await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))

        root = DET.parse(output).getroot()
        # Units 1, 3[0], 3[1] and 4 had no <target>; the fixture's only one is unit 2.
        assert len(root.findall(".//x:target", NS)) == 5
        for element in root.iter():
            assert element.tag.startswith("{" + XLIFF_NS + "}"), element.tag

    async def test_default_namespace_is_kept_without_ns0_prefixes(self, run_job):
        _, output = await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))

        with open(output, encoding="utf-8") as handle:
            text = handle.read()
        assert f'<xliff xmlns="{XLIFF_NS}" version="1.2">' in text
        assert "ns0:" not in text
        assert "xmlns:ns0" not in text

    async def test_stray_unqualified_element_keeps_its_meaning(self, run_job, tmp_path):
        """
        An element undeclared out of the default namespace can't be written under
        xmlns="..." without silently moving it into that namespace, so the output
        falls back to prefixes, which stay correct XML. A namespaced job runs
        first so a default-prefix registration it leaked would show up here.
        """
        await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))
        source = tmp_path / "mixed.xlf"
        source.write_text(
            f'<xliff version="1.2" xmlns="{XLIFF_NS}"><file><body>'
            '<trans-unit id="1"><source>Hello there</source></trans-unit>'
            '<note xmlns="">unqualified</note>'
            "</body></file></xliff>",
            encoding="utf-8",
        )
        job, output = await run_job(str(source))

        assert job["status"] == "completed"
        root = DET.parse(output).getroot()
        assert root.find(".//note") is not None
        assert root.find(".//x:target", NS).text == "DA:Hello there"


class TestNonNamespacedXliff:
    """Transifex exports, which declare no namespace, must come out unchanged."""

    async def test_output_is_byte_identical_to_before_the_fix(self, run_job):
        _, output = await run_job(os.path.join(FIXTURES, "sample2.xlf"))

        with open(os.path.join(FIXTURES, "sample2.expected.xlf"), "rb") as handle:
            expected = handle.read()
        with open(output, "rb") as handle:
            assert handle.read() == expected

    async def test_no_namespace_is_introduced(self, run_job):
        """
        Registering the XLIFF namespace for one job is process-wide, so a
        namespaced job followed by a plain one must not leak xmlns into the second.
        """
        await run_job(os.path.join(FIXTURES, "sample_namespaced.xlf"))
        _, output = await run_job(os.path.join(FIXTURES, "sample2.xlf"))

        with open(output, encoding="utf-8") as handle:
            assert "xmlns" not in handle.read()


class TestZeroTransUnits:
    """A document with nothing to translate is a failure, never a silent success."""

    async def test_zero_units_fails_with_a_clear_error(self, run_job, tmp_path):
        source = tmp_path / "empty.xlf"
        source.write_text(
            '<xliff version="1.2"><file><body></body></file></xliff>', encoding="utf-8"
        )
        job, output = await run_job(str(source))

        assert job["status"] == "failed"
        assert "trans-unit" in job["error"]
        assert job.get("download_url") is None
        assert not os.path.exists(output)
