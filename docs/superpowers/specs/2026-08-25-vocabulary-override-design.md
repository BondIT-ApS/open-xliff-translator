# Vocabulary Override — Design

**Date:** 2026-08-25
**Status:** Approved, ready for implementation planning
**Depends on:** SQLite persistence layer (to be extracted from issue #28)

## Problem

LibreTranslate produces the *dictionary-correct* Danish translation, which is
often the wrong word for the product being translated. `Ban` becomes `Forbyde`
where the product says `Bloker`; `Ticket` becomes `Billet` where the product
keeps `Ticket`. Today there is no way to correct this short of hand-editing
every generated `.xlf`, and the correction is lost on the next run.

The vocabulary must persist across container restarts and image rebuilds, and
must be editable without a code change.

## Goals

- Force chosen target terms for chosen source terms, per target language.
- Support "do not translate" terms (target identical to source).
- Persist the vocabulary across restarts and rebuilds.
- Allow editing from the web UI, plus CSV import/export for backup and seeding.
- Report per job how many overrides fired, so a silently-inert glossary is visible.

## Non-goals

Explicitly out of scope. Each was considered and rejected as unnecessary for the
current need:

- **Morphological inflection.** No lemma-aware Danish generation. Inflected
  forms are entered as separate rows.
- **Regex or wildcard entries.** Every entry is a literal surface form.
- **Per-user glossaries.** The instance shares one vocabulary per language.
- **Authentication.** The instance is assumed to run on a trusted network,
  consistent with every existing endpoint.
- **Translation memory.** Whole-segment overrides are a different feature.
- **Glossary audit history.** No versioning of who changed what.

## Decisions

| Question | Decision |
|---|---|
| Scope | One shared list per target language. No per-user or per-project scoping. |
| Matching | Literal surface forms, whole-word, case-insensitive with case carried to the target. |
| Storage | SQLite in a Docker volume, edited via the web UI, with CSV import/export. |
| Pipeline position | Pre-translation masking on the existing sentinel rail, plus a per-job applied-term count. |

### Why pre-translation masking

The codebase already solves this exact problem for i18n placeholders:
`mask_placeholders` / `restore_placeholders` (`app.py:198-250`) wrap
non-translatable spans in `<xN></xN>` tags and send `format: "html"`, so
LibreTranslate leaves them untouched. A forced term is the same shape of
problem — "do not translate this span, substitute my text."

The alternative, find-and-replace on the Danish output, requires anticipating
every form the engine emits and cannot reliably protect a do-not-translate term
that the engine has already translated *and* inflected. Masking is
deterministic: the result does not depend on what the engine happened to
produce.

The cost of masking is that the engine sees a hole in the sentence, so
surrounding grammar can degrade slightly. This is acceptable because every
generated `<target>` is already stamped `state="needs-review-translation"` for
Transifex, so a human reviews it regardless.

## Architecture

### Module structure

`app.py` is 584 lines. This feature adds roughly 250 lines plus a database
layer. Rather than let a single file reach ~900 lines, the backend is split into
flat modules at the repository root:

| File | Responsibility |
|---|---|
| `app.py` | FastAPI app construction, routes, lifespan. Thin. |
| `settings.py` | The `Settings` class and the loaded `settings` singleton. |
| `db.py` | SQLite connection management, schema creation, migrations. |
| `glossary.py` | Term model, CRUD, regex compilation, match/mask/restore. |
| `translation.py` | `translate_text`, `translate_xliff_with_progress`, placeholder masking. |

Flat modules rather than a package directory, so `uvicorn app:app`, the
`Dockerfile` `CMD`, and all existing import paths are unaffected.

This split is a deliberate departure from the current single-file backend and
carries three knock-on changes that are part of this work:

1. `pylint app.py --rcfile=.pylintrc` in both `.github/workflows/pr-quality-gate.yml`
   and `.github/workflows/docker-publish.yml` must widen to cover all modules.
2. `--cov=app` in `pytest.ini` must widen to name the new modules. The
   `[coverage:run] source = .` block below it looks like it already covers
   them, but the `--cov=app` command-line argument narrows measurement; confirm
   which wins as the first step, because if the new modules go unmeasured the
   70% gate becomes meaningless.
3. `CLAUDE.md` describes a "single-file backend (`app.py`)" and cites specific
   line numbers throughout. Both must be updated.

### New dependency footprint

None. Python's stdlib `sqlite3` is used in WAL mode. Writes are rare (a human
editing terms) and reads happen once per translation job, so there is no need
for `aiosqlite` or an ORM.

## Data model

```sql
CREATE TABLE glossary_terms (
    id          INTEGER PRIMARY KEY,
    target_lang TEXT    NOT NULL,               -- ISO 639-1, e.g. 'da'
    source_term TEXT    NOT NULL,               -- 'Banned'
    target_term TEXT    NOT NULL,               -- 'Blokeret'
    match_case  INTEGER NOT NULL DEFAULT 0,     -- 1 = match exact casing only
    enabled     INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    created_at  TEXT    NOT NULL,               -- ISO 8601 UTC
    updated_at  TEXT    NOT NULL,
    UNIQUE (target_lang, source_term)
);

CREATE INDEX ix_glossary_lang_enabled ON glossary_terms (target_lang, enabled);
```

Notes on the shape:

- **Do-not-translate needs no flag.** `target_term = source_term` expresses it.
  `Ticket → Ticket` masks the span and restores it unchanged.
- **`match_case`** exists for terms whose casing is meaningful, such as the
  acronym `IT` versus the pronoun `it`.
- **`UNIQUE (target_lang, source_term)`** is case-sensitive at the SQL level, so
  `IT` and `it` can coexist as distinct rows. The CRUD layer additionally
  rejects a new case-insensitively-colliding row when the existing row has
  `match_case = 0`, because such a pair would be genuinely ambiguous.

### Migrations

A `schema_version` table holding a single integer, plus an ordered list of
migration statements in `db.py`. On startup, apply every migration with an index
above the stored version, then write the new version. Alembic is disproportionate
for a schema this small.

## Matching algorithm

### Compilation

For a given target language, load all rows with `enabled = 1` and build a single
alternation:

```python
terms.sort(key=lambda t: len(t.source_term), reverse=True)
pattern = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t.source_term) for t in terms) + r")(?!\w)",
    re.IGNORECASE,
)
```

- **Longest-first ordering** makes `Ban User` win over `Ban`, because Python's
  alternation is leftmost-first-alternative rather than longest-match.
- **Lookarounds instead of `\b`**, which is more robust for multi-word terms and
  terms containing punctuation.
- **Compiled once and cached** in a module-level dict keyed by target language,
  invalidated on every glossary write. Recompiling per segment would be
  significant overhead on a file with thousands of `trans-unit` elements.
- **Empty glossary** short-circuits: no pattern is built and masking is skipped
  entirely.

### Resolution

The regex matches case-insensitively, so a matched span must then be resolved to
a specific row. Given matched text `M` in language `L`:

1. If a row exists with `source_term == M` exactly, use it.
2. Otherwise, among rows where `lower(source_term) == lower(M)` and
   `match_case = 0`, use it.
3. Otherwise there is no applicable row: leave the span untouched and do not
   mask it.

Step 3 is what makes `match_case` work. If the only row is `IT` with
`match_case = 1`, then the text `it` matches the regex, fails resolution, and is
left for the engine to translate normally.

### Case transfer

Applied only when the resolved row has `match_case = 0`. When `match_case = 1`,
the target is emitted **verbatim**, because the author specified exact casing
deliberately.

For `match_case = 0`, given matched text `M` and stored `target_term` `T`:

| Condition on `M` | Result |
|---|---|
| All uppercase and `len(M) > 1` | `T.upper()` |
| First character uppercase | `T[0].upper() + T[1:]` |
| Otherwise | `T[0].lower() + T[1:]` |

The third row matters: a mid-sentence `ban` must yield `bloker`, not `Bloker`.
An author who wants a term always capitalized regardless of source casing uses
`match_case = 1`.

## Pipeline integration

### Order of operations in `translate_text`

1. HTML-escape the source and mask **placeholders** — existing behaviour,
   unchanged.
2. Mask **glossary terms** against the placeholder-masked string, continuing the
   same `<xN>` index counter so restoration stays uniform.
3. Evaluate `has_translatable_text` on the result.
4. Call LibreTranslate (skipped — see below — when step 3 is false).
5. Restore all sentinels: placeholder indices to their original literal,
   glossary indices to the case-transferred target term.

Placeholders are masked **first** so a glossary term can never match inside a
placeholder such as `%1$s`.

### Correction required to the early-return guard

`app.py:282` currently returns the **raw original text** when nothing
translatable remains after masking:

```python
if not has_translatable_text(masked_text):
    return text
```

With glossary masking in place this is a silent correctness bug. A segment
consisting only of `Ban` masks to `<x0></x0>`, trips the guard, and returns
`"Ban"` — the override never fires. The guard must instead skip the network call
but still run restoration over the masked string, yielding `"Bloker"`.

The same correction applies to the fallback return at `app.py:326`, and to any
path that returns the untranslated source: **every** return path must run
restoration, or overrides are dropped exactly when the engine is unavailable.

### Applied-term reporting

`mask_glossary` returns the count of spans it masked. `translate_xliff_with_progress`
accumulates this into `jobs[job_id]["terms_applied"]`, and `ProgressResponse`
gains `terms_applied: int`. This makes an inert glossary — wrong language code,
empty table, disabled rows — visible rather than silent.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/glossary?target_lang=da` | List terms for a language |
| `POST` | `/api/glossary` | Create a term |
| `PUT` | `/api/glossary/{id}` | Update a term |
| `DELETE` | `/api/glossary/{id}` | Delete a term |
| `GET` | `/api/glossary/export?target_lang=da` | Download CSV |
| `POST` | `/api/glossary/import` | Upload CSV, `mode=merge\|replace` |

All request and response bodies are Pydantic models, consistent with the
existing endpoints, so they appear correctly in the generated OpenAPI docs.

### CSV format

Header row required. Columns: `source_term,target_term,match_case,enabled,note`.
Read as `utf-8-sig` so a BOM-prefixed export from Excel is accepted — Danish
vocabulary lists are likely to originate in a spreadsheet.

## User interface

A collapsible "Vocabulary" panel added to `templates/index.html`, below the
existing upload form. It contains a table of terms (source, target, match case,
enabled) with an inline add row, per-row delete, and import/export links. The
app keeps its single-page character; no router or second template is introduced.

## Error handling

| Condition | Behaviour |
|---|---|
| Blank or whitespace-only `source_term` | `422`, via a Pydantic validator |
| Duplicate term | `409`, naming the conflicting existing row |
| Unknown `id` on update or delete | `404` |
| CSV row invalid | Row skipped; response reports `{imported, skipped, errors[]}` rather than failing the whole upload |
| CSV `mode=replace` | Runs inside a single transaction, so a failure leaves the previous vocabulary intact |
| **Glossary unreadable at translation time** | **Log the error and translate without overrides.** A broken vocabulary degrades quality; it must not fail the job. |

The last row is the important one. The glossary is an enhancement to
translation, not a precondition for it.

## Configuration

New settings in `settings.py`, mirrored into `.env.template`:

- `database_path: str = "data/glossary.db"`
- `glossary_enabled: bool = True` — a kill switch that bypasses all masking

`docker-compose.yml` gains a third named volume, `openxliff_data:/app/data`.
**Without this the database is lost on every rebuild**, which defeats the entire
purpose of the feature.

`HealthCheckResponse` gains `database: "ok" | "error"`, checked the same way the
filesystem is: a trivial query against the open connection. It follows the
existing degraded-versus-unhealthy logic — a database failure alone is
`degraded`, since translation still works without overrides.

## Testing

Extending `tests/test_app.py`, or splitting into `tests/test_glossary.py`
alongside it.

**Matching:**
- Longest-match-first: `Ban User` wins over `Ban`
- Word boundaries: `Ban` does not match inside `Banner` or `Urban`
- All three case-transfer rules, including mid-sentence lowercase
- `match_case = 1` matches exact casing only and emits the target verbatim
- Resolution step 3: unresolvable case-insensitive match is left untouched
- Do-not-translate: `Ticket → Ticket` survives a round trip
- Term adjacent to a placeholder: `"Ban %1$s now"` preserves both
- A segment consisting only of glossary terms — the `app.py:282` trap
- Empty glossary is a no-op and does not alter existing behaviour

**Persistence and CRUD:**
- Write, close the connection, reopen, read back
- `409` on duplicate; `404` on unknown id; `422` on blank term
- CSV round trip: export then import reproduces the table
- CSV `merge` versus `replace` semantics
- CSV with a UTF-8 BOM and Danish characters (`æ`, `ø`, `å`)

**Regression:**
- The existing placeholder suite must pass unchanged with the glossary disabled.

The `--cov-fail-under=70` gate in `pytest.ini` should be raised as part of this
work rather than diluted by new uncovered modules.

## Rollout

The feature is inert until a term is added: an empty glossary short-circuits
before any regex is built, so translation behaviour is byte-identical to today.
`glossary_enabled=false` provides an explicit kill switch. Existing translated
files are unaffected; overrides apply to new jobs only.

## Issue sequencing

1. **New issue — "Introduce SQLite persistence layer."** Extracted from #28.
   `db.py`, schema versioning, the `openxliff_data` volume, the health check
   field, and the module split.
2. **New issue — "Vocabulary override."** This design. Depends on 1.
3. **#28 re-scoped** to download history alone, built on 1.

Splitting this way means the persistence work is reviewed once and reused,
rather than being invented twice in two feature branches.
