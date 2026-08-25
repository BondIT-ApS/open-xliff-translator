# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Open XLIFF Translator is a Dockerized web-based translation tool that processes XLIFF (.xlf) files using LibreTranslate. The application is a FastAPI backend split across flat modules at the repository root, handling file uploads, XLIFF parsing, translation, vocabulary overrides, and downloads. It integrates with a containerized LibreTranslate service for translation capabilities.

**Key Characteristics:**
- Modern FastAPI application with async/await support and a modular backend
  (`app.py`, `translation.py`, `glossary.py`, `history.py`, `cleanup.py`,
  `validation.py`, `middleware.py`, `db.py`, `settings.py` — flat modules at the repo root)
- SQLite persistence via stdlib `sqlite3` in WAL mode, stored on a Docker volume
- Uses `httpx` for async HTTP requests with connection pooling and retry logic
- Uses `defusedxml` for secure XML parsing to prevent XXE attacks
- Focuses on Transifex-compatible XLIFF format with specific state attributes
- Handles placeholder formatting preservation (e.g., `%1$s`, `%n`) during translation
- Default translation: English to Danish (configurable via `target_lang` parameter)
- Automatic OpenAPI documentation at `/docs` and `/redoc`
- Built-in health checks for monitoring at `/health`

## Development Commands

### Running the Application

```bash
# Start all services (FastAPI app + LibreTranslate)
docker-compose up -d --build

# View logs
docker logs -f open-xliff-translator
docker logs -f open-xliff-libretranslate

# Stop services
docker-compose down
```

**Access Points:**
- FastAPI Application: http://localhost:5003
- API Documentation (Swagger): http://localhost:5003/docs
- API Documentation (ReDoc): http://localhost:5003/redoc
- Health Check: http://localhost:5003/health
- LibreTranslate API: http://localhost:5002

### Linting

```bash
# Install dependencies
pip install -r requirements.txt
pip install pylint

# Run pylint (configured via .pylintrc)
pylint ./*.py --rcfile=.pylintrc
```

Lint every root module, not a hand-maintained list — the list drifted once and
left four modules unlinted in CI.

**Pylint Configuration (.pylintrc):**
- Max line length: 120 characters
- Disabled: docstring requirements, too-few-public-methods warnings
- Ignores: venv directory, httpx, fastapi modules

### Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Run the full test suite with coverage
# pytest.ini already sets testpaths=tests plus the coverage flags,
# so a bare `pytest` snaps every brick into place
pytest

# Generate HTML coverage report
pytest --cov-report=html
# View coverage at htmlcov/index.html
```

**Test Coverage (85%+ required):**
- XLIFF parsing and translation logic
- Placeholder masking and restoration
- File upload/download endpoints
- Health check endpoint
- Error handling (timeouts, malformed files, service failures)
- Security (path traversal prevention, secure filename sanitization)

**CodeCov Integration:**
- Coverage reports uploaded automatically in CI/CD
- PR comments show coverage diff
- Both PR and main builds upload the **same** `unittests` flag. They previously
  used different flags, which meant no commit ever carried both and every PR
  delta rendered as `?`.

## Architecture

### Core Components

1. **Backend modules** — flat files at the repo root, so `uvicorn app:app` and the
   Dockerfile `CMD` never had to change when the single file was split:

   | Module | Responsibility |
   |---|---|
   | `app.py` | FastAPI app construction, routes, lifespan |
   | `translation.py` | Translation pipeline, placeholder masking, the in-memory job store |
   | `db.py` | SQLite connection, WAL, schema versioning, migrations |
   | `settings.py` | The `Settings` class and its loaded singleton |
   | `glossary.py` | Vocabulary overrides: term store, matching, masking |
   | `history.py` | Session-scoped download history |
   | `cleanup.py` | Retention sweep for files and the job store |
   | `validation.py` | Upload size cap and XLIFF structure validation |
   | `middleware.py` | Security headers, CSP, rate limiting |

   `http_client` and `jobs` live in `translation.py` as module-level globals and must
   be referenced as `translation.http_client` — never `from translation import
   http_client`, which would bind `None` permanently at import time and break the
   tests that patch it.

   - Async application handling all backend logic
   - Key async functions:
     - `translate_text()`: Async calls to LibreTranslate API with retry logic and exponential backoff
     - `translate_xliff_with_progress()`: Async XLIFF parsing, translation, target element creation, and job progress
     - `mask_placeholders()`: Swaps i18n placeholders for `<xN></xN>` sentinel tags before translation
     - `restore_placeholders()`: Snaps the original placeholders back in after translation
     - `has_translatable_text()`: Skips strings that are placeholders-only, with nothing worth translating
     - `secure_filename()`: Sanitizes filenames to prevent path traversal attacks
   - Routes:
     - `GET /`: Serves the HTML interface
     - `POST /upload`: File upload and processing endpoint
     - `GET /download/{filename}`: Download translated files
     - `GET /health`: Health check for monitoring
     - `GET /docs`: Auto-generated Swagger API documentation
     - `GET /redoc`: Auto-generated ReDoc API documentation

2. **LibreTranslate Service**
   - Containerized translation engine
   - Configured to load only English and Danish models (see docker-compose.yml)
   - Accessed via internal Docker network at `http://libretranslate:5000/translate`

3. **File Structure**
   - `/uploads`: Temporary storage for uploaded XLIFF files
   - `/processed`: Storage for translated XLIFF files
   - `/data`: SQLite database
   - `/templates/index.html`: Web interface; behaviour in `/static/app.js`, styling in `/static/style.css`
   - All three directories are Docker volumes for persistence

### XLIFF Processing Flow

1. User uploads `.xlf` file via web interface
2. FastAPI saves file securely using `secure_filename()` to `/uploads`
3. `translate_xliff_with_progress()` parses XML using `defusedxml` (secure against XXE)
4. For each `<trans-unit>`:
   - Extracts `<source>` text
   - Masks placeholders into `<xN></xN>` sentinel tags via `mask_placeholders()`
   - Skips the API call entirely when `has_translatable_text()` finds nothing but placeholders
   - Translates via async LibreTranslate API call with `format="html"` and retry logic (3 attempts, exponential backoff)
   - Restores the original placeholders via `restore_placeholders()`
   - Creates or updates `<target>` element
   - Sets `state="needs-review-translation"` attribute for Transifex compatibility
5. Writes translated XLIFF to `/processed` with `translated_` prefix
6. Returns JSON response with download URL

### Docker Architecture

**docker-compose.yml** defines two services:
- `open-xliff-translator`: FastAPI app with Uvicorn (port 5003)
  - Depends on LibreTranslate service (waits for healthy status)
  - Environment: `LOG_LEVEL=INFO`, `PYTHONUNBUFFERED=1`
  - Volumes: `openxliff_uploads`, `openxliff_processed`, `openxliff_data` (SQLite —
    without it the database is lost on every image rebuild)
  - Health check: Curls `/health` endpoint every 30s
- `libretranslate`: Translation engine (port 5002, internal 5000)
  - Only loads English and Danish models (`LT_LOAD_ONLY=en,da`)
  - Health check: Curls `/languages` endpoint every 30s

### Async Architecture

**Connection Management:**
- Single `httpx.AsyncClient` instance shared across requests
- Connection pooling: max 10 connections, 5 keepalive
- Timeout configuration: 30s total, 10s connect
- Proper cleanup via lifespan context manager

**Retry Logic:**
- 3 attempts for LibreTranslate API calls
- Exponential backoff: 1s, 2s, 4s
- Handles `TimeoutException` (504), `HTTPStatusError` (502), generic errors (500)
- Structured logging at each retry attempt

### Health Checks

**`GET /health` endpoint returns:**
- `status`: "healthy" | "degraded" | "unhealthy"
- `libretranslate`: "available" | "unavailable"
- `filesystem`: "writable" | "readonly"
- `database`: "ok" | "error" — a database failure alone is `degraded`, never
  `unhealthy`, since translation still works without it

**Status Logic:**
- **healthy**: All services operational
- **degraded**: One service down (returns 200)
- **unhealthy**: Both services down (returns 503)

**Checks Performed:**
- LibreTranslate: GET `/languages` with 5s timeout
- Filesystem: Write/delete test files in `uploads/` and `processed/`

## Important Implementation Details

### Vocabulary Overrides

Forced terms ride the **same sentinel rail as placeholders**. Order is
load-bearing: `mask_placeholders` runs first, then `glossary.mask_glossary`
against the already-masked string, continuing the same `<xN>` index counter — so
a glossary term can never match inside `%1$s`, and one `restore_placeholders`
call puts both back.

`target_term == source_term` **is** the do-not-translate case; there is no
separate flag. Matching is literal surface forms only (inflections are separate
rows), longest-match-first, word-bounded by lookarounds, case-insensitive with
casing carried to the target. Rows with `match_case` match exact casing and emit
their target verbatim.

`translate_text` returns a `TranslationResult`, **not a `str`** — every return
path must run restoration, including the no-translatable-text early return and
the post-retry fallback, or overrides are silently dropped exactly when the
engine is unavailable.

### Content Security Policy

The app serves a strict CSP with no `'unsafe-inline'`, which means **the template
must contain no inline `<script>`, no `<style>`, no `onclick=`, and no `style=`
attributes** — inline style attributes are blocked by `style-src 'self'` just as
`<style>` blocks are. UI behaviour lives in `static/app.js` and styling in
`static/style.css`.

Adding a UI panel means adding its JS to `static/app.js` and binding with
`addEventListener`; dynamically created controls need event delegation. A guard
in `tests/test_middleware.py` scans the template for inline markup — it is
case-insensitive, because a browser executes `<SCRIPT>` and honours `onClick=`
exactly as it does the lowercase forms.

`/docs` and `/redoc` get a separately scoped policy, matched by exact path so it
can never reach an application response.

### Docker Volume Ownership

Any new directory backed by a volume must be created in the Dockerfile's
`mkdir -p` **before** `chown -R appuser:appuser /app`. Docker seeds a fresh
named volume from the image, so a directory missing from that list arrives
root-owned while the app runs as non-root `appuser`, and the container dies at
startup with `sqlite3.OperationalError: unable to open database file`. This is
invisible to the test suite — it only appears in a real container run.

### Security Considerations

- **XML Parsing**: Always use `defusedxml.ElementTree` for parsing XLIFF files to prevent XXE attacks
- **File Handling**: Use custom `secure_filename()` function for all user-provided filenames
- **File Download**: Verify file existence before serving
- **Path Traversal Prevention**: `secure_filename()` strips path components and sanitizes special characters
- **Input Validation**: Pydantic models validate all request payloads
- **File Extension Validation**: Only `.xlf` files allowed for upload

### Placeholder Masking

Placeholders are the studs that let a translated string click back onto the code that
formats it — so the translation engine is never allowed to touch them. Rather than
repairing damage after the fact, `mask_placeholders()` / `restore_placeholders()` keep
the engine from ever seeing a placeholder:

1. **Mask**: `mask_placeholders()` HTML-escapes the literal text, then replaces each
   placeholder with a numbered sentinel tag — `<x0></x0>`, `<x1></x1>`, and so on —
   returning the masked string plus the list of original placeholder literals.
2. **Translate**: the request goes to LibreTranslate with `format="html"`, so the engine
   treats the sentinels as inline markup to carry across untouched instead of as words
   to translate, reorder, or reformat.
3. **Restore**: `restore_placeholders()` puts each original literal back by index and
   unescapes the result.

Recognised placeholder formats (most specific pattern wins): `%%`, positional printf
(`%1$s`, `%2$d`), plain printf (`%s`, `%d`, `%.2f`, `%02d`, `%@`, `%n`), double-brace
`{{var}}`, template literal `${var}`, and single-brace `{name}` / `{0}`.

Restoration is deliberately forgiving about what comes back: it tolerates self-closing
tags (`<x0/>`), explicit pairs, re-casing, and stray whitespace. Any sentinel tag that
can't be mapped is stripped defensively, so a mangled tag never leaks into the output.

`has_translatable_text()` guards the round trip — once the sentinels are removed, a
string needs a run of at least two letters to be worth sending. This keeps
placeholder-only strings like `%s`, `%dm`, or `{count}` away from the engine, which
tends to drop or mangle short literals sitting next to a placeholder.

### Transifex Compatibility

When creating/updating `<target>` elements:
- Always set `state="needs-review-translation"` attribute
- This signals to Transifex that content needs review
- Critical for proper integration with Transifex workflows

### Translation Configuration

- Default source: `auto` detection
- Default target: `da` (Danish)
- Configurable via `target_lang` parameter in async `translate_xliff_with_progress()`
- LibreTranslate only has English/Danish models loaded (modify `LT_LOAD_ONLY` in docker-compose.yml to add languages)
- All translation calls are async using `httpx.AsyncClient`
- Automatic retry on failure (3 attempts with exponential backoff)

## CI/CD Pipeline

### GitHub Actions Workflows

1. **Docker Build and Push** (`.github/workflows/docker-publish.yml`)
   - Triggers on push to `main` or manual dispatch
   - Builds multi-platform image using Docker Buildx
   - Tags with `latest` and version-date format (e.g., `v1.0.0-20250125`)
   - Pushes to Docker Hub: `maboni82/open-xliff-translator`

2. **LEGO Quality Gate - PR Validation** (`.github/workflows/pr-quality-gate.yml`)
   - Triggers on pull requests to `main` (opened, synchronize, reopened)
   - Runs the following bricks, mostly in parallel:
     - **Discover LEGO Building Plan**: detects which areas changed (backend, frontend,
       workflows), tallies change statistics, and posts a LEGO-themed PR comment
     - **Lint Backend**: Pylint on every root module via `.pylintrc` (non-blocking, uses `|| true`)
     - **Lint Workflows**: actionlint across `.github/workflows/`
     - **Backend Quality**: `pytest` with coverage, uploads to CodeCov, then Bandit and
       Safety CLI security scans (Safety results uploaded as artifacts)
     - **CodeQL Security Analysis**: security scanning for Python and JavaScript/TypeScript
     - **Trivy Container Image Scan**: OS and library CVEs in the built image,
       `ignore-unfixed` so only actionable findings are reported
     - **Backend Assembly Test**: Docker build verification
     - **Quality Gate Summary**: aggregates every brick into one status, including
       open code-scanning alerts. Code scanning reports through its own check,
       outside this workflow's `needs`, so the summary queries it directly — it
       twice announced "All Quality Checks Passed" while a required check was red.
   - **Uses Python 3.14, matching the `python:3.14-slim` base image.** CI must
     exercise the interpreter that actually ships; it previously tested on 3.11.

3. **Weekly Security Report** (`.github/workflows/weekly-security-report.yml`)
   - Scheduled Mondays at 09:00 UTC, plus manual dispatch
   - One weekly digest covering both vulnerabilities and outdated dependencies
   - Python via Safety CLI (falls back to pip-audit without `SAFETY_API_KEY`);
     Node via `npm audit`; freshness via `pip list --outdated` / `npm outdated`
   - Opens or updates a single GitHub issue in place rather than flooding the inbox,
     and auto-closes the previous week's issue once a scan comes back fully clean

## Git Commit and PR Conventions

When creating commits and pull requests:
- Follow conventional commit format (e.g., `fix:`, `feat:`, `security:`, `docs:`)
- Keep commit messages concise and descriptive
- **NEVER** add "Generated with Claude Code" or similar AI attribution signatures to commit messages or PR descriptions
- Only use the Co-Authored-By tag when explicitly requested by the user
- Keep PRs focused on a single issue or feature

## BondIT ApS Branding

This project maintains BondIT ApS branding with LEGO-themed documentation:
- Uses LEGO building metaphors throughout README
- Emphasizes Danish origin and systematic approach
- Playful yet professional tone
- Documentation should maintain this theme when making updates
