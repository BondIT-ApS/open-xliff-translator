"""Tests for the glossary term store."""
import pytest

import db
import glossary


@pytest.fixture
def conn(tmp_path):
    """A migrated, isolated database connection."""
    connection = db.connect(str(tmp_path / "glossary.db"))
    yield connection
    connection.close()


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
