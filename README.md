# LocalReader

A private, local text-to-speech application, developed on a Mac and intended
to run in Docker on a Linux Intel N95 mini PC.

The foundation includes a React/TypeScript frontend with Tailwind CSS,
a FastAPI backend, portable path settings, and a working connection check.
Local Kokoro speech generation powers both a developer command and a voice
preview API. The browser lists installed voices and offers short WAV previews.
The document API extracts text from TXT and PDF uploads. Full text-to-speech
jobs are a later phase.

## Requirements

- Python 3.12.
- Node.js 24 LTS and npm; `.nvmrc` records the recommended Node major.
- Docker with Docker Compose v2 for container development.

Native verification used Python 3.12.11, Node 25.8.2, and npm 11.12.1 on macOS.
The container definitions use Python 3.12 and Node 24 Linux images, without
forcing a CPU architecture. No models, FFmpeg, or database setup are needed
to run the browser/API foundation. Speech generation requires the separate
model download described below.

## Native development

Run all commands below from the project root. Dependencies and temporary files
stay inside the project; a globally activated Python environment is unnecessary.

```sh
mkdir -p .cache/tmp
python3.12 -m venv backend/.venv
TMPDIR="$PWD/.cache/tmp" PIP_DISABLE_PIP_VERSION_CHECK=1 \
  backend/.venv/bin/python -m pip install --no-cache-dir -r backend/requirements-dev.txt
TMPDIR="$PWD/.cache/tmp" npm --prefix frontend ci --cache "$PWD/.cache/npm" --no-audit --no-fund
```

Configuration is optional; the application has usable defaults. To customize:

```sh
if [ ! -f .env ]; then cp .env.example .env; fi
```

Start the backend in one terminal:

```sh
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Start the frontend in a second terminal:

```sh
npm --prefix frontend run dev --cache "$PWD/.cache/npm"
```

Open [LocalReader](http://127.0.0.1:5173). The page should show
**Connected to your local service** and list available voices after model setup.
It makes relative `/api` requests; Vite forwards them to FastAPI. No separate
CORS setup is needed.
The check runs on page load and when you click **Check connection**; it times
out after five seconds rather than polling continuously.

Stop either service with `Ctrl+C` in its terminal. If you stop the backend and
check the connection, the page shows **Local service unavailable**. Restart the
backend and click **Try again** to reconnect.

Direct health check:

```sh
curl --fail http://127.0.0.1:8000/api/health
```

Expected HTTP 200 response:

```json
{"status":"ok"}
```

This endpoint reports API availability. It does not claim that a TTS engine is
installed or ready. Interactive API documentation is at
[the backend docs page](http://127.0.0.1:8000/docs).

## Configuration

The backend loads `.env` from the project root explicitly. Process environment
variables take precedence. Relative directory settings resolve against the
project root, even if the backend is launched from another working directory.
Absolute overrides are also accepted. Reading settings creates no runtime files.

| Variable | Native default | Consumer / container behavior |
| --- | --- | --- |
| `MODEL_DIR` | `models` | Backend model directory; Compose mounts it at `/app/models` read-only. |
| `TEMP_DIR` | `data/temp` | Backend temporary files; Compose mounts it at `/app/data/temp`. |
| `DATABASE_DIR` | `data/database` | Backend database directory; Compose mounts it at `/app/data/database`. |
| `KOKORO_MODEL_PATH` | `MODEL_DIR/kokoro-v1.0.onnx` | Optional native model-file override; relative values use the project root. |
| `KOKORO_VOICES_PATH` | `MODEL_DIR/voices-v1.0.bin` | Optional native voice-bank override; relative values use the project root. |
| `TTS_THREADS` | `2` | CPU inference threads, 1–32; supplied to the container explicitly. |
| `TTS_CHUNK_SIZE` | `800` | Text chunk target in characters, 500–1,000. |
| `MAX_TEXT_CHARS` | `100000` | Maximum raw text length accepted by the text utility, 1–1,000,000. |
| `MAX_UPLOAD_BYTES` | `10000000` | Maximum uploaded file size, 1–50,000,000 bytes. |
| `API_PROXY_TARGET` | `http://127.0.0.1:8000` | Vite server only; Compose always sets `http://backend:8000`. |
| `FRONTEND_PORT` | `5173` | Compose host port only. |
| `BACKEND_PORT` | `8000` | Compose host port only. |

Vite loads `API_PROXY_TARGET` from the root `.env` (and Vite's usual mode-specific
env files); an existing process variable takes precedence. Restart Vite after
changing it. The proxy target stays in the server configuration and is not
included in the browser bundle. Never put credentials in `VITE_*` variables,
because Vite exposes those to the browser.

Compose reads the root `.env` to interpolate host paths and port mappings, then
explicitly supplies container paths and the internal proxy target to services.
Changing a native proxy target does not change container networking. Create any
custom host directories before starting Compose. Keep local `.env` files out
of Git; `.env.example` is safe to track.

The container uses the default versioned filenames inside its `/app/models`
mount. Native `KOKORO_*_PATH` overrides are not injected into Compose; custom
container filenames need corresponding environment entries using container
paths in a Compose override file.

## Local speech generation

After installing the backend dependencies, explicitly download the model and
voice bank (about 354 MB total):

```sh
backend/.venv/bin/python scripts/download_models.py
```

This uses the official Kokoro ONNX `model-files-v1.0` release and stores assets
in `MODEL_DIR`. It verifies sizes and recorded SHA-256 hashes, skips valid
existing files, and never silently replaces existing model files. No model
download happens during application startup or synthesis. Network access is
needed for installation and this setup step; inference uses local CPU execution.

Generate a WAV:

```sh
backend/.venv/bin/python scripts/synthesize.py "Hello, this is LocalReader." --voice heart
```

The command prints a unique filename under `TEMP_DIR`. You can choose a filename
with `--output hello.wav`; existing files are never overwritten. To change
generated speech speed, pass `--speed 1.25`. The supported synthesis range is
0.5–2.0; this is separate from future browser playback speed controls.

Ten provisional English voices are exposed through friendly IDs:

| ID | Display name | Language |
| --- | --- | --- |
| `heart` | Heart | en-US |
| `bella` | Bella | en-US |
| `nicole` | Nicole | en-US |
| `sarah` | Sarah | en-US |
| `sky` | Sky | en-US |
| `adam` | Adam | en-US |
| `michael` | Michael | en-US |
| `emma` | Emma | en-GB |
| `george` | George | en-GB |
| `fable` | Fable | en-GB |

The engine is loaded lazily and shared within each process. Model and voice data
are reused across calls; synthesis runs one call at a time with two CPU threads
by default. Each standalone command is a new process and therefore loads its
own engine. Callers use the `TTSEngine` abstraction, not vendor voice IDs.

These are developer WAV files. Automatic expiration, MP3 output, integration
with long-document jobs, and asynchronous jobs belong to later phases. Remove
test WAVs when finished; the command does not yet implement the application's
30-minute TTL. Use short passages until the document processing pipeline is
implemented.

The app disables ONNX Runtime telemetry before the runtime is imported, and the
backend image also sets `ORT_DISABLE_TELEMETRY=1`. See the upstream
[ONNX Runtime privacy controls](https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md).
Vendor logging that can include input phonemes is disabled. Error messages
returned by the TTS abstraction omit input text and internal model paths.

## Voice preview API

`GET /api/voices` returns the installed voice bank's curated public IDs,
display names, languages, and engine name in a `voices` array. The browser uses
this response for its selector. The first request lazily loads the local model;
if model setup is incomplete, the endpoint returns HTTP 503 with a setup hint.
`/api/health` remains available even when the model is absent.

`POST /api/voices/{voice_id}/preview` synthesizes only “Welcome to LocalReader.
This is a preview of this voice.” It returns `audio/wav` mono PCM with
`Cache-Control: no-store`. The API creates this audio in memory; it does not
write a preview file or accept custom text. Invalid voices return HTTP 404,
missing models HTTP 503, and synthesis failures HTTP 502. For example:

```sh
curl --fail -X POST http://127.0.0.1:8000/api/voices/heart/preview --output preview.wav
```

The browser exposes native audio controls for each requested preview. Changing
voices clears the old player. The `preview.wav` file in the command above is
written by `curl` for manual inspection and must be removed manually; browser
previews remain in memory.

## Text normalization and chunking

The backend utility `app.utils.text.chunk_text()` prepares plain text for later
synthesis. It normalizes Unicode to NFC, joins layout-wrapped lines, collapses
extra whitespace, preserves paragraph breaks, and splits paragraphs at likely
sentence endings. Common abbreviations, initials, and decimal numbers are kept
together where practical. Sentence order is preserved when chunks are packed.

The default target is 800 characters per chunk, configurable from 500 to 1,000
with `TTS_CHUNK_SIZE` or by passing `chunk_size`. Sentences that exceed the
target wrap at word boundaries; a single word longer than the target remains
intact. The utility rejects blank text and input longer than `MAX_TEXT_CHARS`
(100,000 by default; configurable up to 1,000,000). The maximum applies to the
raw input before normalization. The chunking step is not yet connected to a
reader or generation API. Document uploads use the normalization step.

## Document upload API

`POST /api/documents` accepts one multipart file field named `file`. Supported
files are UTF-8 `.txt` and text-based `.pdf` files. The API checks the extension,
declared media type, file size, and PDF signature. It returns normalized text,
character count, and file type:

```sh
curl --fail -F 'file=@example.pdf;type=application/pdf' http://127.0.0.1:8000/api/documents
```

The default file limit is 10,000,000 bytes (`MAX_UPLOAD_BYTES`); extracted text
must fit `MAX_TEXT_CHARS`. Upload data is bounded during multipart parsing,
then copied to a random filename under `TEMP_DIR`. The original file is deleted
immediately after extraction, including when extraction fails. The API does not
retain a document or add a job. Unsupported files return HTTP 415, oversized
files or extracted text HTTP 413, and unreadable files HTTP 422. Scanned PDFs
need OCR, which is outside this phase. The original file bytes are never
returned to the caller.

PDF extraction uses [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/the-basics.html).
Its upstream documentation describes [AGPL and commercial licensing options](https://pymupdf.readthedocs.io/en/latest/about.html);
review those terms before distributing or hosting the application.

The [Kokoro ONNX package](https://github.com/thewh1teagle/kokoro-onnx) is MIT
licensed; the model is Apache 2.0 per upstream. The download script records the
versioned source URLs and hashes of the official assets retrieved during setup;
the release did not publish independent asset digests.

## Docker development

Start Docker Desktop on Mac (or the Docker engine on Linux) first. Stop native
services if they occupy ports 5173/8000, or set alternate Compose host ports.

```sh
docker compose config --quiet
docker compose up --build -d --wait
curl --fail http://127.0.0.1:5173/api/health
```

Open [LocalReader](http://127.0.0.1:5173). Compose waits for the backend's health
check before starting the frontend. Both published ports bind to host loopback
by default. Models and data are project bind mounts; they are not copied into
images. Each image has a restricted build context that excludes local
dependencies, environment files, and caches.

```sh
docker compose logs --tail=100
docker compose down
```

`docker compose down` stops and removes this stack's containers/network; project
bind-mounted files remain. Rebuild after source or dependency changes; source
code is not bind-mounted into these foundation containers.

The frontend container currently runs Vite for local development. Production
frontend serving, FFmpeg, Cloudflare access, and N95
deployment belong to later phases.

Verification status: native backend tests, PDF/TXT upload extraction, frontend
type checking/build, and browser connection/voice-list/preview-decoding checks
pass. Compose configuration validates.
Container builds and startup are still pending because the Docker engine was
not running during verification. The in-app browser crashed when its media Play
control was clicked, so audible playback in that browser remains unverified.

## Checks

```sh
TMPDIR="$PWD/.cache/tmp" backend/.venv/bin/python -m pytest
npm --prefix frontend run build --cache "$PWD/.cache/npm"
docker compose config --quiet
```

The backend tests verify the health contract without runtime files and path
configuration across launch directories, plus text normalization/chunking,
PDF/TXT upload extraction and cleanup, TTS validation, model reuse under
concurrent calls, safe failures, and the voice/preview API. The tests do not
require a model download.
The frontend build includes strict
TypeScript checking. Test temporary directories and outputs are ignored by Git.

## Project layout

```text
.
├── backend/
│   ├── app/                 # FastAPI, settings, services/tts, and text utilities
│   ├── Dockerfile
│   └── requirements*.txt
├── frontend/               # React, Vite, TypeScript, Tailwind
├── models/                 # Local model files (ignored)
├── data/
│   ├── temp/               # Temporary runtime files (ignored)
│   └── database/           # Future SQLite storage (ignored)
├── scripts/                # Explicit model download and native WAV generation
├── tests/                  # Backend checks
├── .env.example
└── docker-compose.yml
```

The product is named LocalReader; the containing folder can retain any name.
Model/data directories retain only `.gitkeep` placeholders in Git. Local
`context.txt`, `phase1.1.txt`, `phase2.1.txt`, `phase3.1.txt`, `phase4.1.txt`, and
`phase5.1.txt` handoff notes are also ignored and must be transferred separately
when another agent uses a different clone or worktree.

## Troubleshooting

- **Port already in use:** stop the other instance. Vite deliberately fails
  instead of silently changing its port. For another native frontend port,
  use `npm --prefix frontend run dev -- --port 5174`. For another backend port,
  change Uvicorn's `--port` and the root `.env` `API_PROXY_TARGET` together,
  then restart Vite. `FRONTEND_PORT`/`BACKEND_PORT` affect Compose only.
- **Service unavailable:** verify the direct backend health URL and terminal
  logs, then check the proxy target and retry. A successful HTTP response with
  an unexpected body is also treated as unavailable.
- **Cannot connect to Docker daemon:** start the engine before building or
  starting the stack. `docker compose config` alone does not verify runtime.

The frontend uses the official [Tailwind Vite integration](https://tailwindcss.com/docs/installation/using-vite)
and [Vite development proxy](https://vite.dev/config/server-options#server-proxy).
