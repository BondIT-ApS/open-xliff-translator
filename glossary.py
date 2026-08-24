"""Vocabulary overrides: persistent forced translation terms."""
import re
import html
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Source terms are matched against HTML-escaped text, so these characters
# would not match reliably. Rejected at creation rather than mishandled later.
FORBIDDEN_IN_SOURCE = ("&", "<", ">")


class Term(NamedTuple):
    """A single vocabulary override."""

    id: int
    target_lang: str
    source_term: str
    target_term: str
    match_case: bool
    enabled: bool
    note: Optional[str]


class InvalidTermError(Exception):
    """The term is structurally invalid."""


class DuplicateTermError(Exception):
    """A conflicting term already exists."""


class TermNotFoundError(Exception):
    """No term with that id."""


def _row_to_term(row: sqlite3.Row) -> Term:
    return Term(
        id=row["id"],
        target_lang=row["target_lang"],
        source_term=row["source_term"],
        target_term=row["target_term"],
        match_case=bool(row["match_case"]),
        enabled=bool(row["enabled"]),
        note=row["note"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate(source_term: str, target_term: str) -> None:
    if not source_term or not source_term.strip():
        raise InvalidTermError("Source term must not be blank")
    if not target_term or not target_term.strip():
        raise InvalidTermError("Target term must not be blank")
    for char in FORBIDDEN_IN_SOURCE:
        if char in source_term:
            raise InvalidTermError(
                f"Source term may not contain '{char}' — matching runs against escaped text"
            )


def _check_conflict(
    conn: sqlite3.Connection,
    target_lang: str,
    source_term: str,
    match_case: bool,
    exclude_id: Optional[int] = None,
) -> None:
    """Reject an exact duplicate, or an ambiguous case-insensitive collision.

    Two rows that both set match_case are unambiguous even when they differ only
    by casing ('IT' and 'it'), because each matches one exact form. Any other
    collision would leave matching undefined.
    """
    rows = conn.execute(
        "SELECT id, source_term, match_case FROM glossary_terms"
        " WHERE target_lang = ? AND lower(source_term) = lower(?)",
        (target_lang, source_term),
    ).fetchall()
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        if row["source_term"] == source_term:
            raise DuplicateTermError(
                f"'{source_term}' already exists for '{target_lang}'"
            )
        if not (match_case and row["match_case"]):
            raise DuplicateTermError(
                f"'{source_term}' collides with existing '{row['source_term']}'"
                f" for '{target_lang}'; set match case on both to keep them distinct"
            )


def list_terms(
    conn: sqlite3.Connection,
    target_lang: Optional[str] = None,
    enabled_only: bool = False,
) -> list[Term]:
    """Return terms, optionally filtered by language and enabled state."""
    query = "SELECT * FROM glossary_terms"
    clauses, params = [], []
    if target_lang is not None:
        clauses.append("target_lang = ?")
        params.append(target_lang)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY source_term COLLATE NOCASE"
    return [_row_to_term(r) for r in conn.execute(query, params).fetchall()]


def get_term(conn: sqlite3.Connection, term_id: int) -> Term:
    """Return one term by id, or raise TermNotFoundError."""
    row = conn.execute(
        "SELECT * FROM glossary_terms WHERE id = ?", (term_id,)
    ).fetchone()
    if row is None:
        raise TermNotFoundError(f"No glossary term with id {term_id}")
    return _row_to_term(row)


def create_term(
    conn: sqlite3.Connection,
    target_lang: str,
    source_term: str,
    target_term: str,
    match_case: bool = False,
    enabled: bool = True,
    note: Optional[str] = None,
    commit: bool = True,
) -> Term:
    """Create a term, rejecting invalid input and conflicts.

    commit=False leaves the transaction open so a bulk import can commit once.
    """
    _validate(source_term, target_term)
    _check_conflict(conn, target_lang, source_term, match_case)
    stamp = _now()
    cursor = conn.execute(
        "INSERT INTO glossary_terms"
        " (target_lang, source_term, target_term, match_case, enabled, note,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_lang,
            source_term,
            target_term,
            int(match_case),
            int(enabled),
            note,
            stamp,
            stamp,
        ),
    )
    if commit:
        conn.commit()
        invalidate_cache()
    return get_term(conn, cursor.lastrowid)


def update_term(conn: sqlite3.Connection, term_id: int, **fields: Any) -> Term:
    """Update the given fields of a term."""
    existing = get_term(conn, term_id)
    merged = existing._replace(
        **{k: v for k, v in fields.items() if k in Term._fields and k != "id"}
    )
    _validate(merged.source_term, merged.target_term)
    _check_conflict(
        conn,
        merged.target_lang,
        merged.source_term,
        merged.match_case,
        exclude_id=term_id,
    )
    conn.execute(
        "UPDATE glossary_terms SET target_lang = ?, source_term = ?, target_term = ?,"
        " match_case = ?, enabled = ?, note = ?, updated_at = ? WHERE id = ?",
        (
            merged.target_lang,
            merged.source_term,
            merged.target_term,
            int(merged.match_case),
            int(merged.enabled),
            merged.note,
            _now(),
            term_id,
        ),
    )
    conn.commit()
    invalidate_cache()
    return get_term(conn, term_id)


def delete_term(conn: sqlite3.Connection, term_id: int) -> None:
    """Delete a term, or raise TermNotFoundError."""
    get_term(conn, term_id)
    conn.execute("DELETE FROM glossary_terms WHERE id = ?", (term_id,))
    conn.commit()
    invalidate_cache()


class CompiledGlossary(NamedTuple):
    """A ready-to-use matcher for one target language."""

    pattern: Optional[re.Pattern]
    exact: dict          # source_term -> Term
    insensitive: dict    # lower(source_term) -> Term, match_case=False only


_cache: dict[str, CompiledGlossary] = {}


def compile_glossary(terms: list[Term]) -> CompiledGlossary:
    """Build one alternation over all enabled terms, longest first.

    Longest-first ordering matters: Python's alternation is
    leftmost-first-alternative, not longest-match, so without it 'Ban' would
    win over 'Ban User'. Lookarounds are used instead of \\b because they
    behave correctly for multi-word and punctuated terms.
    """
    enabled = [t for t in terms if t.enabled]
    if not enabled:
        return CompiledGlossary(None, {}, {})

    ordered = sorted(enabled, key=lambda t: len(t.source_term), reverse=True)
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(t.source_term) for t in ordered) + r")(?!\w)",
        re.IGNORECASE,
    )
    return CompiledGlossary(
        pattern=pattern,
        exact={t.source_term: t for t in ordered},
        insensitive={t.source_term.lower(): t for t in ordered if not t.match_case},
    )


def get_compiled(conn: sqlite3.Connection, target_lang: str) -> CompiledGlossary:
    """Return the cached matcher for a language, compiling it on first use."""
    if target_lang not in _cache:
        _cache[target_lang] = compile_glossary(
            list_terms(conn, target_lang, enabled_only=True)
        )
    return _cache[target_lang]


def invalidate_cache() -> None:
    """Drop every compiled matcher. Called after any write."""
    _cache.clear()
