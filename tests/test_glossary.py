"""Tests for the glossary term store."""
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

import db
import glossary


@pytest.fixture
def conn(tmp_path):
    """A migrated, isolated database connection."""
    connection = db.connect(str(tmp_path / "glossary.db"))
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clear_glossary_cache():
    """Reset the compiled-pattern cache around every test."""
    glossary.invalidate_cache()
    yield
    glossary.invalidate_cache()


class TestCreateTerm:
    """Terms are created, validated, and deduplicated."""

    def test_create_returns_term_with_id(self, conn):
        term = glossary.create_term(conn, "da", "Ban", "Bloker")
        assert term.id > 0
        assert term.source_term == "Ban"
        assert term.target_term == "Bloker"
        assert term.match_case is False
        assert term.enabled is True

    def test_blank_source_rejected(self, conn):
        with pytest.raises(glossary.InvalidTermError):
            glossary.create_term(conn, "da", "   ", "Bloker")

    def test_blank_target_rejected(self, conn):
        with pytest.raises(glossary.InvalidTermError):
            glossary.create_term(conn, "da", "Ban", "")

    def test_markup_characters_in_source_rejected(self, conn):
        for bad in ("R&D", "a<b", "a>b"):
            with pytest.raises(glossary.InvalidTermError):
                glossary.create_term(conn, "da", bad, "X")

    def test_markup_characters_in_target_allowed(self, conn):
        term = glossary.create_term(conn, "da", "RandD", "R&D")
        assert term.target_term == "R&D"

    def test_exact_duplicate_rejected(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        with pytest.raises(glossary.DuplicateTermError):
            glossary.create_term(conn, "da", "Ban", "Andet")

    def test_case_insensitive_collision_rejected(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        with pytest.raises(glossary.DuplicateTermError):
            glossary.create_term(conn, "da", "ban", "Andet")

    def test_two_case_sensitive_variants_allowed(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        term = glossary.create_term(conn, "da", "it", "den", match_case=True)
        assert term.source_term == "it"

    def test_same_term_different_language_allowed(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        term = glossary.create_term(conn, "de", "Ban", "Sperren")
        assert term.target_lang == "de"


class TestListTerms:
    """Listing filters by language and enabled state."""

    def test_filters_by_language(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "de", "Ban", "Sperren")
        assert len(glossary.list_terms(conn, target_lang="da")) == 1

    def test_enabled_only_excludes_disabled(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "da", "Ticket", "Ticket", enabled=False)
        assert len(glossary.list_terms(conn, "da", enabled_only=True)) == 1
        assert len(glossary.list_terms(conn, "da")) == 2


class TestUpdateAndDelete:
    """Terms are updated in place and removed."""

    def test_update_changes_target(self, conn):
        term = glossary.create_term(conn, "da", "Ban", "Forbyde")
        updated = glossary.update_term(conn, term.id, target_term="Bloker")
        assert updated.target_term == "Bloker"

    def test_update_unknown_id_raises(self, conn):
        with pytest.raises(glossary.TermNotFoundError):
            glossary.update_term(conn, 9999, target_term="X")

    def test_delete_removes_term(self, conn):
        term = glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.delete_term(conn, term.id)
        assert glossary.list_terms(conn, "da") == []

    def test_delete_unknown_id_raises(self, conn):
        with pytest.raises(glossary.TermNotFoundError):
            glossary.delete_term(conn, 9999)


class TestPersistence:
    """Terms survive closing and reopening the database."""

    def test_terms_survive_reopen(self, tmp_path):
        path = str(tmp_path / "persist.db")
        connection = db.connect(path)
        glossary.create_term(connection, "da", "Ticket", "Ticket")
        connection.close()

        connection = db.connect(path)
        terms = glossary.list_terms(connection, "da")
        connection.close()
        assert len(terms) == 1
        assert terms[0].source_term == "Ticket"


class TestCompilation:
    """Terms compile into one alternation, longest first."""

    def test_empty_glossary_has_no_pattern(self):
        compiled = glossary.compile_glossary([])
        assert compiled.pattern is None

    def test_disabled_terms_are_excluded(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker", enabled=False)
        compiled = glossary.compile_glossary(glossary.list_terms(conn, "da", enabled_only=True))
        assert compiled.pattern is None

    def test_longest_term_matches_first(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "da", "Ban User", "Bloker bruger")
        compiled = glossary.get_compiled(conn, "da")
        assert compiled.pattern.search("Ban User now").group(1) == "Ban User"

    def test_does_not_match_inside_a_word(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        compiled = glossary.get_compiled(conn, "da")
        assert compiled.pattern.search("Banner") is None
        assert compiled.pattern.search("Urban") is None

    def test_matches_case_insensitively(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        compiled = glossary.get_compiled(conn, "da")
        assert compiled.pattern.search("please ban them") is not None


class TestCache:
    """The compiled pattern is cached and invalidated on write."""

    def test_repeated_calls_return_same_object(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        assert glossary.get_compiled(conn, "da") is glossary.get_compiled(conn, "da")

    def test_create_invalidates_cache(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        first = glossary.get_compiled(conn, "da")
        glossary.create_term(conn, "da", "Ticket", "Ticket")
        assert glossary.get_compiled(conn, "da") is not first

    def test_delete_invalidates_cache(self, conn):
        term = glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.get_compiled(conn, "da")
        glossary.delete_term(conn, term.id)
        assert glossary.get_compiled(conn, "da").pattern is None


class TestApplyCase:
    """Casing is carried from the matched text to the target."""

    def test_all_caps_uppercases_target(self):
        assert glossary.apply_case("BAN", "bloker") == "BLOKER"

    def test_leading_capital_capitalises_target(self):
        assert glossary.apply_case("Ban", "bloker") == "Bloker"

    def test_lowercase_lowercases_target(self):
        assert glossary.apply_case("ban", "Bloker") == "bloker"

    def test_single_uppercase_char_is_not_treated_as_all_caps(self):
        assert glossary.apply_case("A", "bloker") == "Bloker"

    def test_empty_target_is_returned_unchanged(self):
        assert glossary.apply_case("BAN", "") == ""


class TestResolution:
    """A case-insensitive match resolves to exactly one row, or none."""

    def test_exact_match_wins(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        glossary.create_term(conn, "da", "it", "den", match_case=True)
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.resolve(compiled, "IT").target_term == "IT"
        assert glossary.resolve(compiled, "it").target_term == "den"

    def test_case_sensitive_only_row_does_not_catch_other_casing(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.resolve(compiled, "it") is None

    def test_case_insensitive_row_catches_any_casing(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.resolve(compiled, "BAN").target_term == "Bloker"


class TestSubstitute:
    """Substitution applies case transfer, except for match_case rows."""

    def test_case_insensitive_row_transfers_case(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.substitute(compiled, "ban") == "bloker"
        assert glossary.substitute(compiled, "BAN") == "BLOKER"
        assert glossary.substitute(compiled, "Ban") == "Bloker"

    def test_case_sensitive_row_is_verbatim(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.substitute(compiled, "IT") == "IT"

    def test_unresolvable_match_returns_none(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.substitute(compiled, "it") is None

    def test_do_not_translate_term_returns_itself(self, conn):
        glossary.create_term(conn, "da", "Ticket", "Ticket")
        compiled = glossary.get_compiled(conn, "da")
        assert glossary.substitute(compiled, "Ticket") == "Ticket"


import html as html_module

from translation import mask_placeholders, restore_placeholders


def _round_trip(conn, text, target_lang="da"):
    """Mask placeholders then glossary, then restore — no engine involved."""
    masked, originals = mask_placeholders(text)
    compiled = glossary.get_compiled(conn, target_lang)
    masked, count = glossary.mask_glossary(masked, compiled, originals)
    return restore_placeholders(masked, originals), count


class TestMaskGlossary:
    """Masking replaces terms with sentinels that restore to the target."""

    def test_empty_glossary_is_a_noop(self, conn):
        result, count = _round_trip(conn, "Ban the user")
        assert result == "Ban the user"
        assert count == 0

    def test_term_is_replaced(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        result, count = _round_trip(conn, "Ban the user")
        assert result == "Bloker the user"
        assert count == 1

    def test_inflected_forms_are_separate_rows(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "da", "Banned", "Blokeret")
        assert _round_trip(conn, "Ban")[0] == "Bloker"
        assert _round_trip(conn, "Banned")[0] == "Blokeret"

    def test_do_not_translate_term_survives(self, conn):
        glossary.create_term(conn, "da", "Ticket", "Ticket")
        assert _round_trip(conn, "Open a Ticket")[0] == "Open a Ticket"

    def test_placeholder_and_term_both_survive(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        result, count = _round_trip(conn, "Ban %1$s now")
        assert result == "Bloker %1$s now"
        assert count == 1

    def test_term_is_not_matched_inside_a_placeholder(self, conn):
        glossary.create_term(conn, "da", "s", "S")
        result, _ = _round_trip(conn, "Value %1$s here")
        assert "%1$s" in result

    def test_longest_match_wins(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "da", "Ban User", "Bloker bruger")
        assert _round_trip(conn, "Ban User now")[0] == "Bloker bruger now"

    def test_unresolvable_match_is_left_alone(self, conn):
        glossary.create_term(conn, "da", "IT", "IT", match_case=True)
        result, count = _round_trip(conn, "it works")
        assert result == "it works"
        assert count == 0

    def test_target_with_ampersand_survives_restoration(self, conn):
        glossary.create_term(conn, "da", "RandD", "R&D")
        assert _round_trip(conn, "The RandD team")[0] == "The R&D team"

    def test_multiple_terms_counted(self, conn):
        glossary.create_term(conn, "da", "Ban", "Bloker")
        glossary.create_term(conn, "da", "Ticket", "Ticket")
        _, count = _round_trip(conn, "Ban the Ticket")
        assert count == 2



class TestTranslateTextIntegration:
    """translate_text applies overrides and never drops them."""

    @staticmethod
    def _engine(text):
        """A mocked LibreTranslate response returning a distinctive sentinel."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"translatedText": text}
        response.raise_for_status = MagicMock()
        return AsyncMock(return_value=response)

    @pytest.mark.asyncio
    @patch("translation.http_client")
    async def test_segment_of_only_a_term_is_overridden(
        self, mock_client, conn, monkeypatch
    ):
        """A segment left with no translatable text must still get its override.

        The whole segment masks to a sentinel, so the engine is never called;
        returning the raw source here would silently drop the override.
        """
        import db as db_module
        import translation

        mock_client.post = self._engine("ENGINE_OUTPUT")
        glossary.create_term(conn, "da", "Ban", "Bloker")
        monkeypatch.setattr(db_module, "connection", conn)

        result = await translation.translate_text("Ban", "da")
        assert result.text == "Bloker"
        assert result.terms_applied == 1
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    @patch("translation.http_client")
    async def test_glossary_failure_degrades_not_fails(
        self, mock_client, conn, monkeypatch
    ):
        """A broken glossary degrades to a plain translation, never a failed job."""
        import db as db_module
        import translation

        def boom():
            raise RuntimeError("database gone")

        mock_client.post = self._engine("ENGINE_OUTPUT")
        monkeypatch.setattr(db_module, "get_connection", boom)

        result = await translation.translate_text("Ban", "da")
        assert result.text == "ENGINE_OUTPUT"
        assert result.terms_applied == 0

    @pytest.mark.asyncio
    @patch("translation.http_client")
    async def test_kill_switch_bypasses_glossary(
        self, mock_client, conn, monkeypatch
    ):
        """GLOSSARY_ENABLED=false leaves the engine output completely untouched."""
        import db as db_module
        import translation
        from settings import settings

        mock_client.post = self._engine("ENGINE_OUTPUT")
        glossary.create_term(conn, "da", "Ban", "Bloker")
        monkeypatch.setattr(db_module, "connection", conn)
        monkeypatch.setattr(settings, "glossary_enabled", False)

        result = await translation.translate_text("Ban", "da")
        assert result.text == "ENGINE_OUTPUT"
        assert result.terms_applied == 0
