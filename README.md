# 🧱 Open XLIFF Translator

[![Build Status](https://img.shields.io/github/actions/workflow/status/BondIT-ApS/open-xliff-translator/docker-publish.yml?branch=main&style=for-the-badge)](https://github.com/BondIT-ApS/open-xliff-translator/actions)
[![License](https://img.shields.io/github/license/BondIT-ApS/open-xliff-translator?style=for-the-badge)](LICENSE)
[![Repo Size](https://img.shields.io/github/repo-size/BondIT-ApS/open-xliff-translator?style=for-the-badge)](https://github.com/BondIT-ApS/open-xliff-translator)
[![Made in Denmark](https://img.shields.io/badge/made%20in-Denmark%20🇩🇰-red?style=for-the-badge)](https://bondit.dk)
[![codecov](https://codecov.io/gh/BondIT-ApS/open-xliff-translator/branch/main/graph/badge.svg?style=for-the-badge)](https://codecov.io/gh/BondIT-ApS/open-xliff-translator)

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-open--xliff--translator-blue?logo=docker&style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator)
[![Docker Pulls](https://img.shields.io/docker/pulls/maboni82/open-xliff-translator?style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator)

## 🔤 Building Translation Solutions, One Brick at a Time

Welcome to Open XLIFF Translator - where we do for translation workflows what LEGO did for building: make them structured, reliable, and surprisingly enjoyable!

Just like assembling a LEGO masterpiece, we've crafted a solution that transforms complex XLIFF translation workflows into something elegant and straightforward. This Dockerized web-based translation tool uses **FastAPI**, **async Python**, and **LibreTranslate** to create a seamless translation experience. Upload your XLIFF files, watch the magic happen, and download your translated content - all with the precision and reliability you'd expect from a well-engineered LEGO creation.

## 🚀 Features - The Building Blocks

- **📄 XLIFF Translation Magic** – Automatically translate `.xlf` files to different languages, like having a universal translator in your LEGO toolkit
- **⚡ Async Architecture** – Lightning-fast async/await with connection pooling and automatic retry logic
- **🎨 Clean Web Interface** – Upload, process, and download files through an intuitive web interface, as satisfying as that perfect LEGO brick click
- **📚 Auto-Generated API Docs** – Swagger UI and ReDoc documentation built right in, like having the instruction manual always at hand
- **🏥 Health Monitoring** – Built-in health checks for production readiness, ensuring your creation stays sturdy
- **🐳 Dockerized Deployment** – Quick, containerized setup that works everywhere, following clear instructions like a LEGO manual
- **🔓 LibreTranslate Integration** – Powered by open-source LibreTranslate engine, because the best building blocks should be accessible to everyone
- **📖 Vocabulary Overrides** – Force your own house terms onto the translation engine, the custom brick you snap in when the standard piece is the wrong shape
- **🔒 Enterprise Security** – Path validation, secure filename handling, and comprehensive input validation
- **🧪 Well-Tested** – 85%+ test coverage with comprehensive test suite

## 🧱 Getting Started - The Foundation Pieces

### Prerequisites - Tools You'll Need

- [Docker](https://www.docker.com/get-started) - Your primary building tool
- [Docker Compose](https://docs.docker.com/compose/install/) - For connecting the pieces

### Installation - Assembly Instructions

1. **📦 Clone the repository**:
    ```bash
    git clone https://github.com/BondIT-ApS/open-xliff-translator.git
    cd open-xliff-translator
    ```

2. **⚙️ Configure Your Environment** (optional):
    The application comes with sensible defaults, but you can customize settings by creating a `.env` file:
    ```bash
    # Copy the template
    cp .env.template .env

    # Edit with your preferred values
    nano .env
    ```

    **Available Configuration Options:**
    - `LOG_LEVEL`: Logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - `APP_PORT`: FastAPI application port (default: 5003)
    - `UPLOAD_FOLDER` / `PROCESSED_FOLDER`: File storage directories
    - `LIBRETRANSLATE_URL`: Translation service endpoint
    - `DEFAULT_TARGET_LANGUAGE`: Default translation target (ISO 639-1 code, default: da)
    - `HTTP_TIMEOUT` / `MAX_RETRIES`: HTTP client settings

    See `.env.template` for full documentation of all available options.

3. **🚀 Assemble the Solution**:
    ```bash
    docker-compose up -d --build
    ```
    Just like that final satisfying "click" when LEGO pieces connect, your containers are now running!

4. **⏳ Wait for health checks** (about 45 seconds):
    ```bash
    # Watch the logs
    docker logs -f open-xliff-translator
    ```

5. **🎯 Access Your Translator**:
    - **Web Interface**: http://localhost:5003
    - **API Documentation (Swagger)**: http://localhost:5003/docs
    - **API Documentation (ReDoc)**: http://localhost:5003/redoc
    - **Health Check**: http://localhost:5003/health
    - **LibreTranslate**: http://localhost:5002

## 🎮 Usage - Playing with Your Creation

### Web Interface

1. Open **http://localhost:5003** in your browser
2. Upload your `.xlf` file using the clean interface
3. Watch as the file is automatically processed and translated
4. Download your translated file with a single click

It's like building with LEGO - simple steps that create something amazing!

### API Usage

```bash
# Upload and translate a file
curl -X POST http://localhost:5003/upload \
  -F "file=@your-file.xlf"

# Response:
# {
#   "message": "File processed successfully",
#   "download_url": "/download/translated_your-file.xlf"
# }

# Download the translated file
curl -O http://localhost:5003/download/translated_your-file.xlf

# Check service health
curl http://localhost:5003/health
```

### Interactive API Documentation

Visit http://localhost:5003/docs for interactive Swagger UI where you can:
- Test all API endpoints
- View request/response schemas
- See detailed error codes
- Try out file uploads directly in the browser

## 📖 Vocabulary Overrides - Your Custom Bricks

Sometimes the standard piece is simply the wrong shape. LibreTranslate might render **Ban** as *Forbyde* one run and *Udelukke* the next, when your product has always said **Bloker**. The vocabulary is the custom brick you snap in: terms you define always win over the engine.

Open the **Vocabulary Overrides** panel on the web interface and add a row:

| Source | Target | Result |
|---|---|---|
| `Ban` | `Bloker` | "Ban the user" → "Bloker brugeren" |
| `Banned` | `Blokeret` | "Banned" → "Blokeret" |
| `Ticket` | `Ticket` | "Open a Ticket" → "Åbn en Ticket" (left alone) |

### How the bricks click together

Your terms are swapped out **before** the text ever reaches the translation engine, riding the same protective rail that already shields placeholders like `%1$s`. The engine never sees the overridden word, so the result cannot drift with whatever Danish it happened to invent that day. On the way back out, your term is clicked into place.

**Each inflected form is its own row.** There is no clever stemming here, and that is deliberate — `Ban`, `Banned`, and `Banning` are three separate bricks. Literal surface forms only: no wildcards, no regex, no surprises.

**To keep a word untranslated**, make the target identical to the source, as with `Ticket` above. The term is masked, skipped by the engine, and restored exactly as written.

**Casing follows the source automatically.** `ban` → *bloker*, `Ban` → *Bloker*, `BAN` → *BLOKER*. If you need a term emitted verbatim regardless (product names, `IT`, `iOS`), tick **Match case** and it is copied through untouched.

Terms live in SQLite and survive `docker-compose down`, so long as `DATABASE_PATH` points at a mounted volume.

### Bulk building with CSV

Danish vocabulary lists usually start life in a spreadsheet, so the panel speaks CSV:

```
source_term,target_term,match_case,enabled,note
Ban,Bloker,0,1,
Banned,Blokeret,0,1,
Ticket,Ticket,0,1,do not translate
```

- **Export CSV** downloads the current vocabulary
- **Import CSV** merges into it, or replaces it wholesale with **replace existing** ticked
- A BOM-prefixed Excel export is read correctly, and Danish characters (æ, ø, å) survive the round trip
- One malformed row is skipped and reported, never fatal — a 200-row vocabulary should not be rejected over a single typo

```bash
# Straight from the command line
curl -o glossary.csv "http://localhost:5003/api/glossary/export?target_lang=da"
curl -F "file=@glossary.csv" "http://localhost:5003/api/glossary/import?target_lang=da&mode=merge"
```

### The kill switch

Set `GLOSSARY_ENABLED=false` to bypass every override without deleting a single term — the whole feature goes inert and translation behaves exactly as it did before. Handy for isolating whether the vocabulary or the engine is responsible for an odd result.

After each job the interface reports how many overrides were applied, and the same count is available as `terms_applied` on `GET /progress/{job_id}`.

## 🧰 Project Architecture - The Building Design

Just like a well-designed LEGO set, this solution consists of several key components:

```
┌─────────────────────────────────────────────────────┐
│                   FastAPI App                       │
│  ┌────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Web UI    │  │  REST API    │  │  Health    │ │
│  │  (HTML)    │  │  (FastAPI)   │  │  Checks    │ │
│  └────────────┘  └──────────────┘  └────────────┘ │
│         │                │                 │        │
│         └────────────────┴─────────────────┘        │
│                          │                          │
│                   Async Processing                  │
│              (httpx + retry logic)                  │
└──────────────────────┬──────────────────────────────┘
                       │
         ┌─────────────┴─────────────┐
         │   LibreTranslate Engine   │
         │   (Translation Service)   │
         └───────────────────────────┘
```

### Core Components

1. **FastAPI Backend** - Modern async Python API with automatic OpenAPI documentation
   - **Async/Await**: Full async support with httpx client and connection pooling
   - **Retry Logic**: 3 attempts with exponential backoff for resilience
   - **Security**: Werkzeug secure_filename + path validation + defusedxml
   - **Monitoring**: Built-in health checks for LibreTranslate and filesystem

2. **Web Interface** - Simple, responsive HTML/CSS interface for file uploads
   - No complex frontend framework needed
   - Clean, intuitive design
   - Works on desktop and mobile

3. **LibreTranslate Engine** - Open-source translation service
   - Supports 30+ languages
   - Configured for English ↔ Danish by default
   - Easily extensible to other languages

4. **Docker Containers** - Production-ready containerization
   - Multi-stage builds for optimal image size
   - Health checks for both services
   - Automatic dependency management

### Technology Stack

- **Backend**: FastAPI 0.115.0 + Uvicorn (ASGI)
- **HTTP Client**: httpx (async with connection pooling)
- **Security**: werkzeug (filename sanitization) + defusedxml (XXE prevention)
- **Translation**: LibreTranslate (open-source NMT)
- **Testing**: pytest + pytest-cov + pytest-asyncio (85%+ coverage)
- **Quality**: pylint (10/10) + bandit + safety + CodeQL
- **CI/CD**: GitHub Actions with automated testing and Docker Hub publishing

## 🐳 Docker Hub Building Set

Our pre-built Docker image is ready for deployment:

- **Docker Hub**: [maboni82/open-xliff-translator](https://hub.docker.com/r/maboni82/open-xliff-translator)

```bash
# Pull the latest version
docker pull maboni82/open-xliff-translator:latest

# Or use a specific version
docker pull maboni82/open-xliff-translator:26.1.XXX
```

## 🔧 Development - Building Custom Pieces

### Local Development Setup

```bash
# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate  # or `. venv/bin/activate` on macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Run the development server (with hot reload)
uvicorn app:app --host 0.0.0.0 --port 5003 --reload

# Run tests
pytest -v --cov=app

# Run linting
pylint app.py --rcfile=.pylintrc

# Run security scan
bandit -r app.py -x tests/
```

### VS Code Tasks

This project includes pre-configured VS Code tasks for common operations:

- **🚀 Run FastAPI App**: Start development server with hot reload
- **🧪 Run Tests**: Execute test suite with coverage
- **🔍 Lint (Pylint)**: Check code quality
- **🏥 Docker Health Check**: Verify service health
- **🚀 Pre-Push Quality Gate**: Run all checks before pushing

Press `Cmd+Shift+P` (or `Ctrl+Shift+P` on Windows/Linux) → "Run Task" to see all available tasks.

## 📊 Project Status

- ✅ **Tests**: 36/36 passing (100%)
- ✅ **Coverage**: 85.51% (exceeds 70% requirement)
- ✅ **Pylint**: 10.00/10 (perfect score)
- ✅ **Bandit**: 0 security issues
- ✅ **CodeQL**: All security scans passing
- ✅ **CI/CD**: Automated testing and deployment

## 👷‍♂️ Contributing - Join Our Building Team

Contributions are welcome! Feel free to open an issue or submit a pull request. Like any good LEGO enthusiast, we believe more builders create better creations.

1. Fork the repository (like borrowing a few bricks)
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Write tests for your changes (we maintain 70%+ coverage)
4. Ensure all quality checks pass (`pylint`, `pytest`, `bandit`)
5. Commit your changes (`git commit -m 'Add some amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request (show us your creation!)

### Quality Standards

All contributions must meet these standards:
- ✅ Pylint score: 10/10
- ✅ Test coverage: ≥70%
- ✅ All tests passing
- ✅ Security scans passing (bandit, safety)
- ✅ Type hints where appropriate
- ✅ Docstrings for public functions

## 📄 License - The Building Rules

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
Like LEGO, you're free to rebuild and reimagine as you see fit!

## 🛣️ Roadmap

- [ ] File size limits and validation
- [ ] Rate limiting for API endpoints
- [ ] XLIFF XSD schema validation
- [ ] Security headers (helmet middleware)
- [ ] Support for additional languages
- [ ] Batch file processing
- [ ] Translation memory/glossary support

## 🐛 Known Issues

See our [issue tracker](https://github.com/BondIT-ApS/open-xliff-translator/issues) for current issues and feature requests.

---

## 🏢 About BondIT ApS

This project is maintained by [BondIT ApS](https://bondit.dk), a Danish IT consultancy that builds digital solutions one brick at a time. Just like our fellow Danish company LEGO, we believe in building things methodically, with precision and a touch of playfulness. Because the best solutions, like the best LEGO creations, are both functional AND fun!

**Made with ❤️, ☕, and 🧱 by BondIT ApS**
