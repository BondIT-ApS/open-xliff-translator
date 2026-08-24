# Vocabulary Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a persistent, UI-editable vocabulary force chosen Danish terms during translation, so `Ban → Bloker`, `Banned → Blokeret`, and `Ticket → Ticket` survive every run.

**Architecture:** Glossary terms are masked *before* the LibreTranslate call, riding the same `<xN></xN>` sentinel rail that already protects i18n placeholders — the engine never sees an overridden term, so the result cannot depend on what Danish it happened to produce. Terms live in SQLite, are compiled into a single cached regex per target language, and are restored as the case-transferred target on the way out.

**Tech Stack:** Python 3.13, FastAPI, stdlib `sqlite3` and `csv` and `re`, pytest, vanilla JS in a Jinja template.

**Spec:** `docs/superpowers/specs/2026-08-25-vocabulary-override-design.md`

**Issue:** #142
**Depends on:** #141 — the SQLite persistence layer plan must be complete first.

## Global Constraints

- **No new runtime dependencies.** Stdlib `sqlite3`, `csv`, `re`, `html`.
- **Placeholders mask first, glossary second.** A glossary term must never be able to match inside `%1$s`. This ordering is load-bearing, not stylistic.
- **Every return path in `translate_text` must run restoration.** Returning raw source text drops overrides exactly when the engine is unavailable.
- **A glossary failure degrades, never fails.** If the glossary cannot be read, log and translate without overrides.
- **Literal surface forms only.** No morphology, no regex entries, no wildcards.
- **Source terms may not contain `&`, `<`, or `>`.** Matching runs against HTML-escaped text; these characters would not match reliably, so they are rejected at creation with a clear message. Target terms may contain them.
- **Migrations are append-only.** Never edit or reorder an existing entry in `db.MIGRATIONS`.
- **Empty glossary is a no-op.** Existing behaviour must be byte-identical when no terms exist.
- **Commit messages follow conventional commit format.** Never add AI attribution.

---

### Task 1: Glossary schema and CRUD

**Files:**
- Create: `glossary.py`
- Modify: `db.py` (append the first migration)
- Test: `tests/test_glossary.py`

**Interfaces:**
- Consumes: `db.connect`, `db.MIGRATIONS` from #141
- Produces, in module `glossary`:
  - `class Term(NamedTuple)` with fields `id: int`, `target_lang: str`, `source_term: str`, `target_term: str`, `match_case: bool`, `enabled: bool`, `note: Optional[str]`
  - `class DuplicateTermError(Exception)`, `class TermNotFoundError(Exception)`, `class InvalidTermError(Exception)`
  - `def list_terms(conn, target_lang: Optional[str] = None, enabled_only: bool = False) -> list[Term]`
  - `def get_term(conn, term_id: int) -> Term`
  - `def create_term(conn, target_lang, source_term, target_term, match_case=False, enabled=True, note=None, commit=True) -> Term`
    (`commit=False` defers the commit and cache invalidation to the caller, so a
    bulk import can be one transaction)
  - `def update_term(conn, term_id, **fields) -> Term`
  - `def delete_term(conn, term_id: int) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_glossary.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'glossary'`

- [ ] **Step 3: Append the migration**

In `db.py`, replace `MIGRATIONS: list[str] = []` with:

```python
MIGRATIONS: list[str] = [
    # 1 — glossary terms
    """
    CREATE TABLE glossary_terms (
        id          INTEGER PRIMARY KEY,
        target_lang TEXT    NOT NULL,
        source_term TEXT    NOT NULL,
        target_term TEXT    NOT NULL,
        match_case  INTEGER NOT NULL DEFAULT 0,
        enabled     INTEGER NOT NULL DEFAULT 1,
        note        TEXT,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        UNIQUE (target_lang, source_term)
    );
    CREATE INDEX ix_glossary_lang_enabled ON glossary_terms (target_lang, enabled);
    """,
]
```

- [ ] **Step 4: Write the CRUD half of `glossary.py`**

```python
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


def invalidate_cache() -> None:
    """Placeholder until Task 2 introduces the compiled-pattern cache."""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_glossary.py -v --no-cov`
Expected: PASS, 17 tests

- [ ] **Step 6: Commit**

```bash
git add glossary.py db.py tests/test_glossary.py
git commit -m "feat: add glossary term store with validation and conflict rules"
```

---

### Task 2: Compile terms into a cached regex

**Files:**
- Modify: `glossary.py` (append)
- Test: `tests/test_glossary.py` (append)

**Interfaces:**
- Consumes: `Term`, `list_terms` from Task 1
- Produces:
  - `class CompiledGlossary(NamedTuple)` with `pattern: Optional[re.Pattern]`, `exact: dict[str, Term]`, `insensitive: dict[str, Term]`
  - `def compile_glossary(terms: list[Term]) -> CompiledGlossary`
  - `def get_compiled(conn, target_lang: str) -> CompiledGlossary` — cached
  - `def invalidate_cache() -> None` — replaces the Task 1 stub

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary.py`:

```python
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
```

Add this autouse fixture near the top of the file, below the `conn` fixture, so cache state never leaks between tests:

```python
@pytest.fixture(autouse=True)
def clear_glossary_cache():
    """Reset the compiled-pattern cache around every test."""
    glossary.invalidate_cache()
    yield
    glossary.invalidate_cache()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary.py::TestCompilation -v --no-cov`
Expected: FAIL with `AttributeError: module 'glossary' has no attribute 'compile_glossary'`

- [ ] **Step 3: Implement compilation and the cache**

In `glossary.py`, delete the `invalidate_cache` stub from Task 1 and append:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_glossary.py -v --no-cov`
Expected: PASS, 25 tests

- [ ] **Step 5: Commit**

```bash
git add glossary.py tests/test_glossary.py
git commit -m "feat: compile glossary terms into cached per-language matcher"
```

---

### Task 3: Resolve matches and transfer case

**Files:**
- Modify: `glossary.py` (append)
- Test: `tests/test_glossary.py` (append)

**Interfaces:**
- Consumes: `CompiledGlossary` from Task 2
- Produces:
  - `def apply_case(matched: str, target: str) -> str`
  - `def resolve(compiled: CompiledGlossary, matched: str) -> Optional[Term]`
  - `def substitute(compiled: CompiledGlossary, matched: str) -> Optional[str]` — the final replacement text, or `None` when no row applies

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary.py::TestApplyCase -v --no-cov`
Expected: FAIL with `AttributeError: module 'glossary' has no attribute 'apply_case'`

- [ ] **Step 3: Implement resolution and case transfer**

Append to `glossary.py`:

```python
def apply_case(matched: str, target: str) -> str:
    """Carry the casing of the matched source text onto the target term.

    The lowercase branch matters: a mid-sentence 'ban' must produce 'bloker',
    not 'Bloker'. An author who wants a term capitalised regardless of source
    casing uses match_case, which bypasses this function entirely.
    """
    if not target:
        return target
    if len(matched) > 1 and matched.isupper():
        return target.upper()
    if matched[0].isupper():
        return target[0].upper() + target[1:]
    return target[0].lower() + target[1:]


def resolve(compiled: CompiledGlossary, matched: str) -> Optional[Term]:
    """Find the row that applies to a matched span.

    1. An exact source_term match wins, whatever its match_case setting.
    2. Otherwise a case-insensitive row for the same lowercased form applies.
    3. Otherwise nothing applies — the span is left for the engine.
    """
    term = compiled.exact.get(matched)
    if term is not None:
        return term
    return compiled.insensitive.get(matched.lower())


def substitute(compiled: CompiledGlossary, matched: str) -> Optional[str]:
    """Return the replacement text for a matched span, or None if none applies."""
    term = resolve(compiled, matched)
    if term is None:
        return None
    if term.match_case:
        return term.target_term
    return apply_case(matched, term.target_term)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_glossary.py -v --no-cov`
Expected: PASS, 37 tests

- [ ] **Step 5: Commit**

```bash
git add glossary.py tests/test_glossary.py
git commit -m "feat: add glossary match resolution and case transfer"
```

---

### Task 4: Mask glossary terms in the translation pipeline

The heart of the feature, and where the `translate_text` early-return trap gets fixed.

**Files:**
- Modify: `glossary.py` (append `mask_glossary`)
- Modify: `translation.py` (`translate_text`)
- Modify: `settings.py` (`glossary_enabled`)
- Modify: `.env.template`
- Test: `tests/test_glossary.py` and `tests/test_app.py:596,623`

**Interfaces:**
- Consumes: `substitute` from Task 3, `mask_placeholders` / `restore_placeholders` from #141
- Produces:
  - `glossary.mask_glossary(text: str, compiled: CompiledGlossary, originals: list[str]) -> tuple[str, int]` — appends replacements to `originals` and returns the masked text plus the number of spans masked
  - `translation.TranslationResult(NamedTuple)` with `text: str` and `terms_applied: int`
  - `translation.translate_text(...) -> TranslationResult` — **the return type changes from `str`**

**Why replacements are HTML-escaped on insertion:** `restore_placeholders` substitutes entries from `originals` and then runs `html.unescape` over the whole string. A target term containing `&` would otherwise be corrupted, so it is escaped going in and unescaped coming out. Placeholders are not escaped because their pattern cannot contain `&`, `<`, or `>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary.py::TestMaskGlossary -v --no-cov`
Expected: FAIL with `AttributeError: module 'glossary' has no attribute 'mask_glossary'`

- [ ] **Step 3: Implement `mask_glossary`**

Append to `glossary.py`:

```python
def mask_glossary(
    text: str, compiled: CompiledGlossary, originals: list
) -> tuple[str, int]:
    """Replace glossary terms with sentinel tags, appending replacements.

    Runs on text that mask_placeholders has already escaped and masked, so a
    term can never match inside a placeholder. Replacements are appended to the
    same `originals` list the placeholders use, continuing the index counter,
    so restore_placeholders handles both uniformly.

    Replacements are HTML-escaped on insertion because restore_placeholders
    unescapes the whole string on the way out.
    """
    if compiled.pattern is None:
        return text, 0

    count = 0

    def _replace(match: re.Match) -> str:
        nonlocal count
        matched = match.group(1)
        replacement = substitute(compiled, matched)
        if replacement is None:
            return match.group(0)
        index = len(originals)
        originals.append(html.escape(replacement, quote=False))
        count += 1
        return f"<x{index}></x{index}>"

    return compiled.pattern.sub(_replace, text), count
```

- [ ] **Step 4: Add the kill switch to settings**

In `settings.py`, under the Translation settings section:

```python
    glossary_enabled: bool = True
```

Append to `.env.template` under Translation Settings:

```
# Apply vocabulary overrides during translation. Set to false to bypass the
# glossary entirely without deleting any terms.
GLOSSARY_ENABLED=true
```

- [ ] **Step 5: Write the failing pipeline tests**

Append to `tests/test_glossary.py`:

```python
class TestTranslateTextIntegration:
    """translate_text applies overrides and never drops them."""

    @pytest.mark.asyncio
    async def test_segment_of_only_a_term_is_overridden(self, conn, monkeypatch):
        """The app.py:282 trap: no translatable text left, but the override must fire."""
        import db as db_module
        import translation

        glossary.create_term(conn, "da", "Ban", "Bloker")
        monkeypatch.setattr(db_module, "connection", conn)

        result = await translation.translate_text("Ban", "da")
        assert result.text == "Bloker"
        assert result.terms_applied == 1

    @pytest.mark.asyncio
    async def test_glossary_failure_degrades_not_fails(self, conn, monkeypatch):
        import db as db_module
        import translation

        def boom():
            raise RuntimeError("database gone")

        monkeypatch.setattr(db_module, "get_connection", boom)
        result = await translation.translate_text("Ban", "da")
        assert result.terms_applied == 0

    @pytest.mark.asyncio
    async def test_kill_switch_bypasses_glossary(self, conn, monkeypatch):
        import db as db_module
        import translation
        from settings import settings

        glossary.create_term(conn, "da", "Ban", "Bloker")
        monkeypatch.setattr(db_module, "connection", conn)
        monkeypatch.setattr(settings, "glossary_enabled", False)

        result = await translation.translate_text("Ban", "da")
        assert result.text == "Ban"
        assert result.terms_applied == 0
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/test_glossary.py::TestTranslateTextIntegration -v --no-cov`
Expected: FAIL with `AttributeError: 'str' object has no attribute 'text'`

- [ ] **Step 7: Rewrite `translate_text`**

In `translation.py`, add to the imports:

```python
from typing import NamedTuple

import db
import glossary
```

Add above `translate_text`:

```python
class TranslationResult(NamedTuple):
    """The translated text plus how many glossary overrides were applied."""

    text: str
    terms_applied: int
```

Replace the body of `translate_text` with:

```python
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
            await asyncio.sleep(2**attempt)
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
```

- [ ] **Step 8: Update the caller and the two existing tests**

In `translate_xliff_with_progress`, change:

```python
                translated_text = await translate_text(source.text, target_lang)
```

to:

```python
                result = await translate_text(source.text, target_lang)
                translated_text = result.text
```

In `tests/test_app.py`, line 596 and line 623, append `.text` to the call result:

```python
        result = (await translate_text("You have %s messages from {owner}", "da")).text
```

```python
        result = (await translate_text("%dm", "da")).text
```

- [ ] **Step 9: Run the full suite**

```bash
pytest -v --no-cov
GLOSSARY_ENABLED=false pytest tests/test_app.py -v --no-cov
```

Expected: PASS both times. The second run is the spec's regression requirement —
the pre-existing placeholder suite must behave identically with the glossary
switched off, proving the feature is genuinely inert when disabled.

- [ ] **Step 10: Commit**

```bash
git add glossary.py translation.py settings.py .env.template tests/
git commit -m "feat: apply vocabulary overrides via pre-translation masking"
```

---

### Task 5: Report applied terms per job

**Files:**
- Modify: `translation.py` (`translate_xliff_with_progress`)
- Modify: `app.py` (`ProgressResponse`, `upload_file`)
- Test: `tests/test_app.py` (append to `TestProgressEndpoint`)

**Interfaces:**
- Consumes: `TranslationResult.terms_applied` from Task 4
- Produces: `jobs[job_id]["terms_applied"]: int` and `ProgressResponse.terms_applied: int`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py` inside `TestProgressEndpoint`:

```python
    def test_progress_reports_terms_applied(self, client):
        job_id = "terms-test-job"
        jobs[job_id] = {"status": "completed", "completed": 5, "total": 5,
                        "download_url": "/download/x.xlf", "error": None,
                        "terms_applied": 7, "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.json()["terms_applied"] == 7
        finally:
            jobs.pop(job_id, None)

    def test_progress_defaults_terms_applied_to_zero(self, client):
        job_id = "terms-default-job"
        jobs[job_id] = {"status": "pending", "completed": 0, "total": 0,
                        "download_url": None, "error": None, "task": None}
        try:
            response = client.get(f"/progress/{job_id}")
            assert response.json()["terms_applied"] == 0
        finally:
            jobs.pop(job_id, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_app.py::TestProgressEndpoint -v --no-cov`
Expected: FAIL with `KeyError: 'terms_applied'`

- [ ] **Step 3: Accumulate the count**

In `translation.py`, inside `translate_xliff_with_progress`, initialise before the loop:

```python
        jobs[job_id]["terms_applied"] = 0
```

and inside the loop, after `translated_text = result.text`:

```python
                jobs[job_id]["terms_applied"] += result.terms_applied
```

- [ ] **Step 4: Surface it in the API**

In `app.py`, extend the model:

```python
class ProgressResponse(BaseModel):
    status: str
    completed: int
    total: int
    terms_applied: int = 0
    download_url: Optional[str] = None
    error: Optional[str] = None
```

In `get_progress`, add:

```python
        terms_applied=job.get("terms_applied", 0),
```

In `upload_file`, add `"terms_applied": 0,` to the initial `jobs[job_id]` dict.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -v --no-cov`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add translation.py app.py tests/test_app.py
git commit -m "feat: report applied vocabulary term count per job"
```

---

### Task 6: REST endpoints for glossary CRUD

**Files:**
- Modify: `app.py` (models + four routes)
- Test: `tests/test_glossary_api.py`

**Interfaces:**
- Consumes: `glossary` CRUD from Task 1, `db.get_connection` from #141
- Produces: `TermIn`, `TermUpdate`, `TermOut` Pydantic models and the four routes below

| Method | Path | Success | Errors |
|---|---|---|---|
| `GET` | `/api/glossary` | 200, list | — |
| `POST` | `/api/glossary` | 201, term | 409 duplicate, 422 invalid |
| `PUT` | `/api/glossary/{id}` | 200, term | 404, 409, 422 |
| `DELETE` | `/api/glossary/{id}` | 204 | 404 |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_glossary_api.py`:

```python
"""Tests for the glossary REST endpoints."""
import pytest
from fastapi.testclient import TestClient

import db
import glossary
from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client backed by an isolated database."""
    connection = db.connect(str(tmp_path / "api.db"))
    monkeypatch.setattr(db, "connection", connection)
    monkeypatch.setattr(db, "get_connection", lambda: connection)
    glossary.invalidate_cache()
    yield TestClient(app)
    connection.close()
    glossary.invalidate_cache()


class TestCreate:
    def test_create_returns_201(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"},
        )
        assert response.status_code == 201
        assert response.json()["target_term"] == "Bloker"

    def test_duplicate_returns_409(self, client):
        payload = {"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"}
        client.post("/api/glossary", json=payload)
        assert client.post("/api/glossary", json=payload).status_code == 409

    def test_blank_source_returns_422(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "  ", "target_term": "Bloker"},
        )
        assert response.status_code == 422

    def test_ampersand_in_source_returns_422(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "R&D", "target_term": "X"},
        )
        assert response.status_code == 422


class TestList:
    def test_list_filters_by_language(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        client.post("/api/glossary", json={"target_lang": "de", "source_term": "Ban", "target_term": "Sperren"})
        assert len(client.get("/api/glossary?target_lang=da").json()) == 1

    def test_list_without_filter_returns_all(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        client.post("/api/glossary", json={"target_lang": "de", "source_term": "Ban", "target_term": "Sperren"})
        assert len(client.get("/api/glossary").json()) == 2


class TestUpdateDelete:
    def test_update_changes_target(self, client):
        term_id = client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Forbyde"}).json()["id"]
        response = client.put(f"/api/glossary/{term_id}", json={"target_term": "Bloker"})
        assert response.status_code == 200
        assert response.json()["target_term"] == "Bloker"

    def test_update_unknown_returns_404(self, client):
        assert client.put("/api/glossary/9999", json={"target_term": "X"}).status_code == 404

    def test_delete_returns_204(self, client):
        term_id = client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"}).json()["id"]
        assert client.delete(f"/api/glossary/{term_id}").status_code == 204
        assert client.get("/api/glossary").json() == []

    def test_delete_unknown_returns_404(self, client):
        assert client.delete("/api/glossary/9999").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary_api.py -v --no-cov`
Expected: FAIL with 404s — the routes do not exist.

- [ ] **Step 3: Add models and routes to `app.py`**

Add to the imports:

```python
from typing import List
from fastapi import Response
import glossary
```

Add the models beside the existing ones:

```python
class TermIn(BaseModel):
    target_lang: str = "da"
    source_term: str
    target_term: str
    match_case: bool = False
    enabled: bool = True
    note: Optional[str] = None


class TermUpdate(BaseModel):
    target_lang: Optional[str] = None
    source_term: Optional[str] = None
    target_term: Optional[str] = None
    match_case: Optional[bool] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


class TermOut(BaseModel):
    id: int
    target_lang: str
    source_term: str
    target_term: str
    match_case: bool
    enabled: bool
    note: Optional[str] = None
```

Add the routes:

```python
@app.get("/api/glossary", response_model=List[TermOut])
async def list_glossary(target_lang: Optional[str] = None):
    """List vocabulary overrides, optionally filtered by target language."""
    terms = glossary.list_terms(db.get_connection(), target_lang)
    return [TermOut(**t._asdict()) for t in terms]


@app.post("/api/glossary", response_model=TermOut, status_code=201)
async def create_glossary_term(payload: TermIn):
    """Create a vocabulary override."""
    try:
        term = glossary.create_term(
            db.get_connection(),
            payload.target_lang,
            payload.source_term,
            payload.target_term,
            payload.match_case,
            payload.enabled,
            payload.note,
        )
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except glossary.DuplicateTermError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info("Created glossary term: %s -> %s", term.source_term, term.target_term)
    return TermOut(**term._asdict())


@app.put("/api/glossary/{term_id}", response_model=TermOut)
async def update_glossary_term(term_id: int, payload: TermUpdate):
    """Update a vocabulary override."""
    fields = payload.model_dump(exclude_none=True)
    try:
        term = glossary.update_term(db.get_connection(), term_id, **fields)
    except glossary.TermNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except glossary.DuplicateTermError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return TermOut(**term._asdict())


@app.delete("/api/glossary/{term_id}", status_code=204)
async def delete_glossary_term(term_id: int):
    """Delete a vocabulary override."""
    try:
        glossary.delete_term(db.get_connection(), term_id)
    except glossary.TermNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return Response(status_code=204)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_glossary_api.py -v --no-cov`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_glossary_api.py
git commit -m "feat: add glossary CRUD endpoints"
```

---

### Task 7: CSV import and export

**Files:**
- Modify: `glossary.py` (append)
- Modify: `app.py` (two routes)
- Test: `tests/test_glossary_api.py` (append)

**Interfaces:**
- Consumes: `create_term`, `list_terms`, `delete_term` from Task 1
- Produces:
  - `glossary.export_csv(conn, target_lang: Optional[str]) -> str`
  - `glossary.import_csv(conn, content: str, target_lang: str, mode: str) -> dict` returning `{"imported": int, "skipped": int, "errors": list[str]}`
  - Routes `GET /api/glossary/export` and `POST /api/glossary/import`

Columns: `source_term,target_term,match_case,enabled,note`. Read as `utf-8-sig` so a BOM-prefixed Excel export works — Danish vocabulary lists usually start life in a spreadsheet.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary_api.py`:

```python
class TestCsvExport:
    def test_export_includes_header_and_rows(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        body = client.get("/api/glossary/export?target_lang=da").text
        assert "source_term,target_term,match_case,enabled,note" in body
        assert "Ban,Bloker" in body

    def test_export_is_a_csv_attachment(self, client):
        response = client.get("/api/glossary/export?target_lang=da")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]


class TestCsvImport:
    def _upload(self, client, text, mode="merge", lang="da"):
        return client.post(
            f"/api/glossary/import?target_lang={lang}&mode={mode}",
            files={"file": ("terms.csv", text.encode("utf-8"), "text/csv")},
        )

    def test_import_creates_terms(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\nTicket,Ticket,0,1,keep\n"
        response = self._upload(client, csv_text)
        assert response.status_code == 200
        assert response.json()["imported"] == 2
        assert len(client.get("/api/glossary?target_lang=da").json()) == 2

    def test_import_tolerates_utf8_bom(self, client):
        csv_text = "﻿source_term,target_term,match_case,enabled,note\nBlokeret,Blokeret,0,1,\n"
        assert self._upload(client, csv_text).json()["imported"] == 1

    def test_import_preserves_danish_characters(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nAccount,Brugerændring,0,1,\n"
        self._upload(client, csv_text)
        assert client.get("/api/glossary?target_lang=da").json()[0]["target_term"] == "Brugerændring"

    def test_invalid_row_is_skipped_not_fatal(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n,Missing,0,1,\n"
        body = self._upload(client, csv_text).json()
        assert body["imported"] == 1
        assert body["skipped"] == 1
        assert len(body["errors"]) == 1

    def test_merge_keeps_existing_terms(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ticket", "target_term": "Ticket"})
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n"
        self._upload(client, csv_text, mode="merge")
        assert len(client.get("/api/glossary?target_lang=da").json()) == 2

    def test_replace_clears_existing_terms(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ticket", "target_term": "Ticket"})
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n"
        self._upload(client, csv_text, mode="replace")
        terms = client.get("/api/glossary?target_lang=da").json()
        assert len(terms) == 1
        assert terms[0]["source_term"] == "Ban"

    def test_round_trip_reproduces_the_table(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        exported = client.get("/api/glossary/export?target_lang=da").text
        self._upload(client, exported, mode="replace")
        terms = client.get("/api/glossary?target_lang=da").json()
        assert len(terms) == 1
        assert terms[0]["target_term"] == "Bloker"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_glossary_api.py::TestCsvExport -v --no-cov`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Implement import and export in `glossary.py`**

Add `import csv` and `import io` to the imports, then append:

```python
CSV_COLUMNS = ["source_term", "target_term", "match_case", "enabled", "note"]


def export_csv(conn: sqlite3.Connection, target_lang: Optional[str] = None) -> str:
    """Serialise terms to CSV text."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for term in list_terms(conn, target_lang):
        writer.writerow(
            {
                "source_term": term.source_term,
                "target_term": term.target_term,
                "match_case": int(term.match_case),
                "enabled": int(term.enabled),
                "note": term.note or "",
            }
        )
    return buffer.getvalue()


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def import_csv(
    conn: sqlite3.Connection, content: str, target_lang: str, mode: str = "merge"
) -> dict:
    """Import terms from CSV text.

    A bad row is skipped and reported rather than failing the whole upload —
    a 200-row vocabulary should not be rejected over one typo. 'replace' runs
    inside a transaction, so a failure leaves the previous vocabulary intact.
    """
    if mode not in ("merge", "replace"):
        raise InvalidTermError("mode must be 'merge' or 'replace'")

    content = content.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)

    imported, skipped, errors = 0, 0, []
    try:
        if mode == "replace":
            conn.execute(
                "DELETE FROM glossary_terms WHERE target_lang = ?", (target_lang,)
            )

        for number, row in enumerate(rows, start=2):
            try:
                create_term(
                    conn,
                    target_lang,
                    (row.get("source_term") or "").strip(),
                    (row.get("target_term") or "").strip(),
                    _truthy(row.get("match_case"), False),
                    _truthy(row.get("enabled"), True),
                    (row.get("note") or "").strip() or None,
                    commit=False,
                )
                imported += 1
            except (InvalidTermError, DuplicateTermError) as e:
                skipped += 1
                errors.append(f"Row {number}: {e}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        invalidate_cache()

    logger.info(
        "Glossary import (%s): %d imported, %d skipped", mode, imported, skipped
    )
    return {"imported": imported, "skipped": skipped, "errors": errors}
```

`commit=False` is what makes `replace` honest. If `create_term` committed per
row, the first successful insert would also commit the `DELETE`, and a later
failure would leave the vocabulary half-erased — the opposite of what the spec
promises. Deferring to a single `conn.commit()` means a failure rolls back the
`DELETE` too, and the previous vocabulary survives intact.

- [ ] **Step 4: Add the routes to `app.py`**

These must be declared **before** `/api/glossary/{term_id}`, or FastAPI matches `export` as a `term_id` and returns 422.

```python
@app.get("/api/glossary/export")
async def export_glossary(target_lang: Optional[str] = None):
    """Download the vocabulary as CSV."""
    body = glossary.export_csv(db.get_connection(), target_lang)
    suffix = target_lang or "all"
    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="glossary_{suffix}.csv"'
        },
    )


@app.post("/api/glossary/import")
async def import_glossary(
    target_lang: str = "da",
    mode: str = "merge",
    file: UploadFile = File(...),
):
    """Upload a CSV vocabulary, merging into or replacing the current one."""
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422, detail="File must be UTF-8 encoded"
        ) from e
    try:
        result = glossary.import_csv(db.get_connection(), content, target_lang, mode)
    except glossary.InvalidTermError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return JSONResponse(content=result)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_glossary_api.py -v --no-cov`
Expected: PASS, 20 tests

- [ ] **Step 6: Commit**

```bash
git add glossary.py app.py tests/test_glossary_api.py
git commit -m "feat: add glossary CSV import and export"
```

---

### Task 8: Vocabulary panel in the UI, plus documentation

**Files:**
- Modify: `templates/index.html`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `pytest.ini` (add `--cov=glossary`)
- Modify: `.github/workflows/pr-quality-gate.yml`, `.github/workflows/docker-publish.yml` (add `glossary.py` to pylint)

- [ ] **Step 1: Add the panel markup**

In `templates/index.html`, after the closing tag of the preview section, insert:

```html
<details id="vocabPanel">
    <summary><h3 style="display:inline">Vocabulary Overrides</h3></summary>
    <p>Terms here always win over the translation engine. Enter each inflected
       form as its own row &mdash; <code>Ban</code>, <code>Banned</code>,
       <code>Banning</code>. To keep a word untranslated, make the target
       identical to the source.</p>

    <table id="vocabTable">
        <thead>
            <tr><th>Source</th><th>Target</th><th>Match case</th><th>Enabled</th><th></th></tr>
        </thead>
        <tbody id="vocabRows"></tbody>
        <tfoot>
            <tr>
                <td><input id="newSource" placeholder="Ban"></td>
                <td><input id="newTarget" placeholder="Bloker"></td>
                <td><input type="checkbox" id="newMatchCase"></td>
                <td><input type="checkbox" id="newEnabled" checked></td>
                <td><button type="button" onclick="addTerm()">Add</button></td>
            </tr>
        </tfoot>
    </table>

    <p id="vocabError" style="color:#c00"></p>
    <p>
        <a href="/api/glossary/export?target_lang=da" download>Export CSV</a>
        &nbsp;|&nbsp;
        <label>Import CSV:
            <input type="file" id="vocabImport" accept=".csv" onchange="importTerms()">
        </label>
        <label><input type="checkbox" id="vocabReplace"> replace existing</label>
    </p>
</details>
```

- [ ] **Step 2: Add the panel script**

Inside the existing `<script>` block:

```javascript
const VOCAB_LANG = 'da';

async function loadTerms() {
    const rows = document.getElementById('vocabRows');
    const response = await fetch(`/api/glossary?target_lang=${VOCAB_LANG}`);
    const terms = await response.json();
    rows.innerHTML = '';
    terms.forEach(term => {
        const tr = document.createElement('tr');
        [term.source_term, term.target_term,
         term.match_case ? 'yes' : '', term.enabled ? 'yes' : ''].forEach(value => {
            const td = document.createElement('td');
            td.textContent = value;
            tr.appendChild(td);
        });
        const actions = document.createElement('td');
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = 'Delete';
        remove.onclick = () => deleteTerm(term.id);
        actions.appendChild(remove);
        tr.appendChild(actions);
        rows.appendChild(tr);
    });
}

function showVocabError(message) {
    document.getElementById('vocabError').textContent = message || '';
}

async function addTerm() {
    showVocabError('');
    const response = await fetch('/api/glossary', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            target_lang: VOCAB_LANG,
            source_term: document.getElementById('newSource').value,
            target_term: document.getElementById('newTarget').value,
            match_case: document.getElementById('newMatchCase').checked,
            enabled: document.getElementById('newEnabled').checked
        })
    });
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showVocabError(typeof body.detail === 'string' ? body.detail : 'Could not add term');
        return;
    }
    document.getElementById('newSource').value = '';
    document.getElementById('newTarget').value = '';
    loadTerms();
}

async function deleteTerm(id) {
    await fetch(`/api/glossary/${id}`, {method: 'DELETE'});
    loadTerms();
}

async function importTerms() {
    showVocabError('');
    const input = document.getElementById('vocabImport');
    if (!input.files.length) { return; }
    const mode = document.getElementById('vocabReplace').checked ? 'replace' : 'merge';
    const form = new FormData();
    form.append('file', input.files[0]);
    const response = await fetch(
        `/api/glossary/import?target_lang=${VOCAB_LANG}&mode=${mode}`,
        {method: 'POST', body: form}
    );
    const body = await response.json();
    if (body.errors && body.errors.length) {
        showVocabError(`${body.imported} imported, ${body.skipped} skipped: ${body.errors[0]}`);
    }
    input.value = '';
    loadTerms();
}

loadTerms();
```

- [ ] **Step 3: Show the applied-term count**

In the existing progress-polling handler, where completion is reported, append:

```javascript
        if (data.terms_applied > 0) {
            statusElement.textContent += ` (${data.terms_applied} vocabulary overrides applied)`;
        }
```

Substitute the real element variable used by the existing code.

- [ ] **Step 4: Verify the panel by hand**

```bash
docker-compose up -d --build
```

Open http://localhost:5003, expand **Vocabulary Overrides**, and confirm:
- Adding `Ban` / `Bloker` shows a row
- Adding `ban` / `X` shows the 409 message rather than a silent failure
- Export downloads a CSV containing the row
- Uploading that CSV with **replace** ticked leaves exactly one row
- Translating a file containing "Ban the user" produces "Bloker" in the `<target>`, and the progress line reports the override count

- [ ] **Step 5: Widen coverage and lint**

In `pytest.ini`, add `--cov=glossary` to the `--cov` list. Then run
`pytest --cov-report=term-missing` and raise `--cov-fail-under` to the new
reported TOTAL rounded **down** to the nearest 5. The spec requires the gate to
rise as tests are added, never to be diluted by new modules. In both workflow
files, add `glossary.py` to the pylint invocation:

```yaml
          pylint app.py db.py glossary.py settings.py translation.py --rcfile=.pylintrc
```

- [ ] **Step 6: Document the feature**

In `README.md`, add a **Vocabulary Overrides** section in the established LEGO voice — the vocabulary is the custom brick you snap in when the standard piece is the wrong shape. Cover: what it does, the three example terms, that inflected forms are separate rows, do-not-translate via identical source and target, CSV import/export, and the `GLOSSARY_ENABLED` kill switch.

In `CLAUDE.md`, add `glossary.py` to the module table, document the placeholders-then-glossary masking order as load-bearing, and note that `translate_text` returns `TranslationResult`, not `str`.

- [ ] **Step 7: Run the full suite with the gate enabled**

Run: `pytest`
Expected: PASS with coverage at or above the gate.

- [ ] **Step 8: Commit**

```bash
git add templates/index.html README.md CLAUDE.md pytest.ini .github/workflows/
git commit -m "feat: add vocabulary panel to UI and document overrides"
```

---

## Definition of Done

- [ ] `Ban` → `Bloker`, `Banned` → `Blokeret`, `Ticket` → `Ticket` verified end to end
- [ ] Terms survive `docker-compose down && docker-compose up -d --build`
- [ ] `BAN` → `BLOKER`, `ban` → `bloker`; `match_case` rows emit verbatim
- [ ] `Ban User` beats `Ban`; `Ban` does not match inside `Banner`
- [ ] `"Ban %1$s now"` preserves both the override and the placeholder
- [ ] A segment consisting only of a glossary term is overridden
- [ ] Empty glossary is a no-op; every pre-existing test passes unchanged
- [ ] `GLOSSARY_ENABLED=false` bypasses all masking
- [ ] A database failure logs a warning and translates without overrides
- [ ] CSV round trip reproduces the table, BOM and Danish characters intact
- [ ] `terms_applied` visible in `/progress` and in the UI
- [ ] `pytest` passes with the coverage gate enforced
- [ ] No new entries in `requirements.txt`
