# FastAPIProject — Living Learning & Documentation Record

> Source-of-truth doc. Rendered to `FastAPIProject_Overview.pdf` on request.
> Started: 2026-08-13 (Session 1). Preserve history — append, don't overwrite.

---

## 1. Project Overview

A PDF-based Retrieval-Augmented Generation (RAG) backend. A user's PDFs are
chunked and embedded, stored in a vector DB, and questions are answered by
retrieving relevant chunks and asking Gemini to answer using only that
context, with citations back to source chunks.

Goal of this phase: fix known issues in the working pipeline, then evolve it
into a clean FastAPI REST API — incrementally, via reviewed branches/PRs —
while documenting the engineering reasoning at each step.

## 2. Architecture (current, Session 1 baseline)

```
                 ┌─────────────────────────┐
 event: rag/     │   Inngest Function       │
 ingest_pdf ────▶│   rag_ingest_pdf         │
                 │   step 1: _load  ────────┼──▶ data_loader.load_and_chunk_pdf
                 │   step 2: _upsert ───────┼──▶ data_loader.embed_texts (Gemini)
                 │                          │       └─▶ vector_db.QdrantStorage.upsert
                 └─────────────────────────┘

                 ┌─────────────────────────┐
 event: rag/     │   Inngest Function       │
 ingest_pdf_ai ─▶│   rag_query_pdf_ai       │
                 │   step 1: _search ───────┼──▶ vector_db.QdrantStorage.search
                 │   step 2: llm-answer ────┼──▶ Gemini (via inngest ai.gemini adapter)
                 └─────────────────────────┘

 FastAPI app only exposes the Inngest webhook surface
 (inngest.fast_api.serve) — there is no direct REST API yet.
```

There is **no HTTP endpoint a human can call directly** — the app only
serves the Inngest sync/webhook route. Events currently have to be sent via
Inngest (e.g., dev server UI or SDK), not curl/Streamlit. This is a known
gap addressed in Step 5/6 (REST API design).

## 3. Technology Stack

| Tool | Role |
|---|---|
| FastAPI | ASGI web app, currently only hosting the Inngest handler |
| Uvicorn | ASGI server |
| Inngest | Durable function orchestration (steps, retries, event triggers) |
| google-genai | Gemini embeddings (`gemini-embedding-001`) + generation |
| Qdrant (qdrant-client) | Vector storage + similarity search |
| llama-index-core / -readers-file | PDF loading (`PDFReader`) + chunking (`SentenceSplitter`) |
| Pydantic | Typed data contracts between pipeline stages |
| uv | Dependency + environment management (`pyproject.toml` + `uv.lock`) |
| Streamlit | Declared dependency, **not yet used anywhere** |

## 4. Project Structure

```
FastAPIProject/
├── pyproject.toml, uv.lock       # uv-managed deps
├── README.md                     # currently empty
├── .gitignore                    # excludes .venv, .env, __pycache__ — correct
├── test.pdf                      # sample input for manual testing
└── src/fastapiproject/
    ├── __init__.py
    ├── main.py                   # FastAPI app + 2 Inngest functions
    ├── data_loader.py            # PDF load/chunk + Gemini embeddings
    ├── vector_db.py              # QdrantStorage: upsert + search
    └── custom_types.py           # Pydantic models for inter-step data
```

**Git state at Session 1 start (important):** the repo has never had a
completed initial commit. `custom_types.py`, `data_loader.py`,
`vector_db.py` are staged as *empty* files (from an earlier partial `git
add`) with all real content sitting unstaged; `main.py`, `__init__.py`,
`README.md`, `pyproject.toml`, `uv.lock`, `.gitignore` are untracked
entirely. **Decision:** Step 3 will start by making one clean baseline
commit of the current working code on `main` before any feature/fix
branches are cut — see [§18 Questions & Decisions](#18-questions--decisions).

## 5. RAG Ingestion Flow (`rag_ingest_pdf`)

1. Triggered by Inngest event `rag/ingest_pdf`, payload: `{"pdfs": [{"pdf_path": str, "source_id": str?}, ...]}`.
2. **Step "Load and Chunk pdf"** (`_load`): for each pdf, `PDFReader().load_data(Path(pdf_path))` → page texts → `SentenceSplitter(chunk_size=1000, chunk_overlap=200).split_text()` → flat list of chunks + matching source ids. Returns `RagChunkAndSource`.
3. **Step "Embedding and upsert"** (`_upsert`): `embed_texts(chunks)` calls Gemini `gemini-embedding-001`. Each chunk gets a deterministic id via `uuid.uuid5(NAMESPACE_URL, f"{source}:{index}")` — re-ingesting the same source/index pair overwrites rather than duplicates. `QdrantStorage().upsert(ids, vecs, payloads)` where payload = `{source, text}`. Returns `RagUpsertResult(ingested=n)`.
4. Inngest wraps each step with automatic retry or resumability if it fails — this is the main reason business logic is expressed as `ctx.step.run(...)` instead of plain function calls.

## 6. RAG Query Flow (`rag_query_pdf_ai`)

1. Triggered by Inngest event `rag/ingest_pdf_ai` *(naming inconsistency — see finding F7)*, payload: `{"question": str, "top_k": int?, "score_threshold": float?}`.
2. **Step "searching"** (`_search`): embeds the question, `QdrantStorage().search(query_vec, top_k, score_threshold)` → cosine similarity search in Qdrant, returns matched chunk texts + their sources.
3. Contexts are numbered (`[1] ... [2] ...`) and inserted into a prompt instructing Gemini to answer **only** from context and end with a machine-parseable `Used: [n, n, ...]` citation line.
4. `ctx.step.ai.infer(...)` calls Gemini (model `gemini-3.6-flash` — **unverified, see finding F2**) through Inngest's AI step wrapper (gives Inngest visibility/retries over the LLM call itself).
5. Response is regex-parsed to split the prose answer from the `Used: [...]` line; cited indices are mapped back to `found.sources`, deduplicated, and returned as `{"answers", "sources", "num_contexts"}`.

## 7. FastAPI Architecture (Step 4)

### 7.1 The `app` object, ASGI, and Uvicorn
`app = FastAPI(lifespan=lifespan)` (`main.py`) creates an **ASGI application object**. ASGI (Asynchronous Server Gateway Interface) is the async successor to WSGI — it lets a Python web app handle async I/O, streaming, and (though unused here) websockets. FastAPI is built on Starlette, which implements ASGI; FastAPI adds routing sugar, Pydantic-based validation, and automatic OpenAPI docs on top.

`app` on its own does nothing — it's just an object with a registered set of routes/middleware/lifespan hooks. **Uvicorn is the ASGI server**: a real process that opens a socket, speaks HTTP, and for each incoming request calls into `app` using the ASGI calling convention (`scope`, `receive`, `send`). The standard way to run this project is:
```
uv run uvicorn fastapiproject.main:app --reload
```
**Finding F11 (new, discovered this session):** `pyproject.toml` declares a console script `fastapiproject = "fastapiproject:main"`, but `src/fastapiproject/__init__.py` is empty — there is no `main` attribute on the package, so running the installed `fastapiproject` command (or `uv run fastapiproject`) fails immediately with an `AttributeError`/import error. It does **not** start the app. Confirmed via `hasattr(fastapiproject, 'main')` → `False`. This is cosmetic (the real launch command above works fine) but misleading for anyone who tries the "obvious" `uv run fastapiproject`. Logged for a future fix, not addressed yet — see §24.

### 7.2 Where the routes actually come from
This project defines **zero routes directly** (no `@app.get(...)` / `@app.post(...)` anywhere in `main.py`). All routes come from this one line:
```python
inngest.fast_api.serve(app, inngest_client, [rag_ingest_pdf, rag_query_pdf_ai])
```
Reading `inngest.fast_api.serve`'s source confirms it calls `@app.get(...)`, `@app.post(...)`, and `@app.put(...)` on the `app` object we pass in, all at the same path — `/api/inngest` by default (`inngest._internal.const.DEFAULT_SERVE_PATH`). So after this line runs, `app` has exactly three routes, all owned by the Inngest SDK, not by our own code:
- `GET /api/inngest` — introspection (lets the Inngest Dev Server/Cloud ask "what functions do you serve?").
- `PUT /api/inngest` — registration ("sync"): tells Inngest about `rag_ingest_pdf`/`rag_query_pdf_ai` so it knows they exist and what events trigger them.
- `POST /api/inngest` — invocation: Inngest calls this to actually run a step of one of our functions.

### 7.3 Request lifecycle, end to end
For the two RAG functions, there is **no direct client → our-app HTTP call**. The actual path is:
1. Something sends an event (`rag/ingest_pdf` or `rag/ingest_pdf_ai`) to Inngest — e.g. `inngest_client.send(...)` from other code, or the Inngest Dev Server UI. This does **not** hit our FastAPI app at all; it hits Inngest's own event API.
2. Inngest's orchestrator (running separately — the Dev Server locally, or Inngest Cloud in production) sees a matching trigger and makes an HTTP `POST /api/inngest` call **into our FastAPI app** to execute the function.
3. Uvicorn accepts the TCP connection, parses the HTTP request into an ASGI `scope`, and calls `app`.
4. On the very first request after process start, FastAPI runs our `lifespan` context manager (§F4) — `_validate_required_env_vars()` — before any route handler runs. If that raises, the app never starts serving at all.
5. Starlette's router matches the path (`/api/inngest`) and method (`POST`) to the handler `inngest.fast_api.serve` registered, and calls it.
6. That handler hands the request to Inngest's `CommHandler`, which figures out *which* function and *which step* this call is for, and calls into our `rag_ingest_pdf`/`rag_query_pdf_ai` code.
7. **Key detail specific to Inngest ("durable execution"):** one HTTP call ≠ one full function run. Inngest calls `POST /api/inngest` **once per step**. On each call, our function body re-executes from the top, but every `ctx.step.run(...)` whose step already completed returns its previously-saved (memoized) result immediately instead of re-running the lambda — it doesn't repeat the work. The first step that hasn't run yet actually executes; its result is packaged up and the call returns; Inngest saves that result and makes another `POST /api/inngest` call for the next step. This is why `_load`, `_upsert`, `_search` must be deterministic and side-effect-safe to re-run, and why raising a clear exception from inside a step (see F1/F3/F4 fixes) is the correct way to signal failure — Inngest catches it and retries that step, exactly the "durable/retryable" behavior the whole architecture is built around.
8. Once the function returns its final value, that becomes the *Inngest function's* result (visible in the Inngest Dev Server UI / via its API) — not an HTTP response body a REST client is waiting on. Nothing in this project today lets an external caller wait synchronously for `{"answers": ..., "sources": ...}` over HTTP — that gap is exactly what Step 5/6 (a real `/ask` endpoint) will close.

### 7.4 Pydantic models — current role vs. future role
`custom_types.py`'s models (`RagChunkAndSource`, `RagUpsertResult`, `RagSearchResult`, `QueryResult`) are **not** used as FastAPI request/response schemas today, because there are no custom routes for them to attach to. Their actual job right now: `output_type=` on `ctx.step.run(...)`, combined with `inngest.PydanticSerializer()` on the client — this tells Inngest how to serialize a step's Python return value to JSON for durable storage, and deserialize it back into a real Pydantic object (with validation) when a memoized result is replayed. That's a genuine, if different, use of Pydantic's validation: it guards the *replay* path, not an incoming HTTP request.

In Step 5/6, these same models (or close variants) become the natural request/response schemas for real endpoints — e.g. a `POST /ask` request body validated against a `QueryRequest(question: str, top_k: int = 5, ...)` model, and `QueryResult` returned directly as the response, with FastAPI auto-generating the OpenAPI schema and rejecting malformed requests with `422` before our code ever runs.

### 7.5 Validation today (a gap, not yet in §12's F-list)
Because there's no Pydantic model on the *incoming* side, event payloads are read via raw dict access — `ctx.event.data["pdfs"]`, `.get("source_id", pdf_path)`, `ctx.event.data["question"]`. A malformed event (missing `"question"` key, `"pdfs"` not a list, etc.) raises a raw `KeyError`/`TypeError` deep inside the function rather than a clean validation error. This is the direct FastAPI-shaped fix Step 5 provides "for free": a `BaseModel` request schema makes this class of bug structurally impossible for anything going through the new REST endpoint (though the Inngest-event entry point would still need its own explicit validation if kept).

### 7.6 Dependency injection — not used yet
FastAPI's `Depends()` lets a route declare a reusable "give me X" dependency (e.g. a DB session, the current user) that FastAPI resolves and injects before calling the handler; it can chain (dependencies can themselves have dependencies) and is cached per-request by default. Nothing in this project uses it yet, because there are no hand-written routes. Natural fit for later: `get_storage` (added in the F5 fix, `vector_db.py`) is already shaped exactly like a FastAPI dependency function (no arguments, returns a shared instance) — Step 5/6 can likely reuse it as `store: QdrantStorage = Depends(get_storage)` with zero changes to `get_storage` itself.

### 7.7 Response handling, error handling, HTTP status codes — deferred to Step 5/6
Today these are entirely Inngest's concern, not ours: `inngest.fast_api.serve`'s handler decides what HTTP status/body the `/api/inngest` routes return, per Inngest's own wire protocol — our code never calls `HTTPException`, sets a `status_code`, or shapes an HTTP response directly. There's genuinely nothing to teach here yet using *this* codebase, because we haven't written a route. Step 5 is where this becomes concrete: choosing `200` vs `201` for ingestion, `404`/`422` for a bad query, and where a real `try/except` → `raise HTTPException(...)` pattern will replace "let it bubble up to Inngest's retry logic" (which is correct for the *event-driven* pipeline, but wrong for a synchronous REST endpoint — a REST caller needs an immediate, well-shaped error response, not a retry three minutes later).

## 8. API Design (Step 5) — implementation in progress (Step 6)

**Status:** §8.2 (the `rag_service.py` module) is **✅ implemented** (Session 7, `feature/rag-service-extraction`, resolves F9) — see §12/§13 F9. All three §8.3 endpoints are now **✅ implemented**: `POST /ask` (Session 8, `feature/ask-endpoint`, see §12/§13 F5/F8) and `POST /ingest` + `GET /ingest/{event_id}/status` (Session 9, `feature/ingest-endpoint`). Only `feature/api-tests` (broader endpoint test coverage, resolves F6) remains in §19's branch stack.

**Chosen shape (user decision, Session 6):** mixed model — `/ask` is synchronous (calls the RAG pipeline directly, answers immediately); `/ingest` stays asynchronous via the existing Inngest event (`rag/ingest_pdf`), backed by a new status-polling endpoint. This was a genuine architectural fork with real trade-offs (see the three options offered) — recorded so a future reader understands *why* the two endpoints behave so differently, rather than assuming it's an inconsistency.

### 8.1 What existing functionality becomes API functionality

| Existing logic | Today | New endpoint |
|---|---|---|
| `_load` + `_upsert` inside `rag_ingest_pdf` | Only triggerable by sending an `rag/ingest_pdf` Inngest event | `POST /ingest` — sends the same event, adds real file upload instead of requiring a pre-existing server-side path |
| `_search` inside `rag_query_pdf_ai` | Only triggerable by sending an `rag/ingest_pdf_ai` Inngest event | `POST /ask` — calls the retrieval logic directly, no event involved |
| Gemini generation + citation parsing inside `rag_query_pdf_ai` | Runs via `ctx.step.ai.infer` (an Inngest-managed durable step) | `POST /ask` gets its **own** direct Gemini call — see Decision D5, this is *not* a shared code path with the Inngest function |
| (new) Inngest run status | Only visible in the Inngest Dev Server UI | `GET /ingest/{event_id}/status` |

### 8.2 New shared module — `rag_service.py` (resolves F9)

`_load`, `_upsert`, `_search` currently live as closures nested inside the two Inngest function bodies in `main.py` (finding F9). Both the Inngest functions and the new REST endpoints need this logic, so it moves to a new `src/fastapiproject/rag_service.py`:

- `load_and_chunk_sources(pdfs: list[PdfRef]) -> RagChunkAndSource` — today's `_load` body, unchanged logic.
- `embed_and_upsert(chunks_and_src: RagChunkAndSource) -> RagUpsertResult` — today's `_upsert` body, unchanged logic (already hardened by F3).
- `search_context(question: str, top_k: int, score_threshold: float) -> RagSearchResult` — today's `_search` body, unchanged logic (already benefits from F5's `get_storage()` singleton).
- `build_prompt(question: str, contexts: list[str]) -> str` and `parse_cited_answer(raw_answer: str, sources: list[str]) -> tuple[str, list[str]]` — extracted from the inline prompt-building/regex-parsing code currently sitting directly in `rag_query_pdf_ai`, so `/ask` doesn't duplicate that logic.

`main.py`'s `rag_ingest_pdf` and `rag_query_pdf_ai` keep their exact current behavior — they just call into `rag_service.py` instead of defining the logic inline. **No behavior change to the working Inngest functions; this is a pure extraction**, consistent with "don't unnecessarily rewrite working code."

### 8.3 Endpoints

#### `POST /ingest`
- **Consumes:** `multipart/form-data` — `files: list[UploadFile]` (one or more PDFs).
- **Validation:** at least one file; each file's extension is `.pdf` and content-type is `application/pdf`; each file ≤ 20MB (configurable constant) — closes the "no upload size limits" known limitation. Empty file list or all-invalid-type → `422`. Oversized file → `413`.
- **Process:**
  1. For each uploaded file: sanitize the client-supplied filename to just its basename (`Path(file.filename).name`, discarding any directory components — this is the upload-side equivalent of F1's path-traversal defense, since a malicious filename like `../../evil.pdf` must not escape the upload directory either), prefix it with a `uuid4().hex` to avoid collisions, and save it under a new `PDF_UPLOAD_ROOT/uploads/` directory.
  2. Re-validate the saved path through F1's existing `resolve_safe_pdf_path()` before using it — belt-and-suspenders, even though we generated the path ourselves.
  3. Build the same event payload shape `rag_ingest_pdf` already expects (`{"pdfs": [{"pdf_path": ..., "source_id": <original filename>}, ...]}`) and call `inngest_client.send(...)` — **the Inngest function itself is untouched**; this endpoint is just a new way to trigger the same event.
- **Response:** `202 Accepted`, body `{"event_id": str, "status_url": str}`.
- **Errors:** `422` (no files / wrong file type), `413` (file too large), `500` (unexpected failure saving files or sending the event).
- **New config needed:** `PDF_UPLOAD_ROOT/uploads/` must exist before first use (create at `lifespan` startup, alongside the existing env-var check from F4); this directory holds runtime data and must be added to `.gitignore`, same reasoning as `qdrant_storage/` in the baseline commit.

#### `GET /ingest/{event_id}/status`
- **Process:** calls Inngest's own REST API — `GET {INNGEST_API_ORIGIN}/v1/events/{event_id}/runs` — confirmed via Inngest's official docs (`events/ListEventRuns`) rather than assumed; returns an array of run objects, each with a `status` field (`Queued` / `Running` / `Completed` / `Failed` / `Cancelled`). The Dev Server (local) doesn't require the `Authorization: Bearer <signing_key>` header that Inngest Cloud does; `inngest_client.api_origin`/`event_api_origin` already hold the configured origin, so no new env var is needed to build the URL.
- **Response mapping:** `200` with `{"event_id", "status", "ingested": int | None, "error": str | None}` — `ingested` populated from the run's output once `status == "Completed"`; `error` populated from the run's error once `status == "Failed"`.
- **No-runs-yet case:** treated as `status: "Queued"` with `200`, not `404` — right after `POST /ingest` returns, Inngest may not have created the run yet, and a client polling immediately shouldn't see a spurious 404.
- **Errors:** `502`/`503` if Inngest's own API is unreachable (distinguishes "your ingestion failed" from "we can't currently check").

#### `POST /ask`
- **Consumes:** JSON — new request model `AskRequest(question: str, top_k: int = 5, score_threshold: float = 0.5)` in `custom_types.py`.
- **Validation (via Pydantic, enforced automatically — closes the §7.5 gap for this endpoint):** `question` non-empty (`min_length=1`); `top_k` in `[1, 20]`; `score_threshold` in `[0.0, 1.0]`. A violation returns FastAPI's standard `422` with a field-level error body — no custom error handling needed for this part.
- **Process:** calls `rag_service.search_context(...)` directly (no Inngest event); if `contexts` comes back empty, **short-circuits** with a clean "no relevant context found" answer instead of sending an empty context block to Gemini — this finally resolves deferred finding **F8**, and it's natural to fix here since this code path is being written fresh anyway (not true of the existing Inngest function, which is left alone). Otherwise builds the prompt via `rag_service.build_prompt(...)` and calls Gemini directly via the plain `google-genai` client (see Decision D5 for why this isn't `ctx.step.ai.infer`), then parses the citation line via `rag_service.parse_cited_answer(...)`.
- **Response:** `200` with the existing `QueryResult` model (`answers`, `sources`, `num_contexts`) — reused as-is, no new response model needed.
- **Errors:** `422` (bad request body, automatic); `500` (Gemini call fails); `503` (Qdrant unreachable — `get_storage()`/`QdrantClient` raises a connection error).

### 8.4 Status code summary

| Code | Meaning here |
|---|---|
| `200` | `/ask` success; `/ingest/.../status` success (including "still queued") |
| `202` | `/ingest` accepted, processing async |
| `413` | Uploaded file exceeds the size limit |
| `422` | Validation failure (bad request body/files) — FastAPI's default for Pydantic validation errors |
| `500` | Unexpected server-side failure (Gemini/Inngest send error) |
| `502`/`503` | A dependency (Inngest API, Qdrant) is unreachable |

### 8.5 Routing organization
Given the app is about to grow past "everything in `main.py`," endpoints are grouped with FastAPI's `APIRouter` (e.g. `src/fastapiproject/routers.py`: one `APIRouter` for `/ingest`-prefixed routes, one for `/ask`), included into `app` via `app.include_router(...)`. This is the idiomatic way FastAPI apps scale past a handful of routes — a real concept to learn here, not overkill for three endpoints, since it keeps `main.py` focused on Inngest wiring and `routers.py` focused on HTTP concerns.

## 9. Python Concepts Learned

- **Session 1:** `uuid.uuid5(namespace, name)` — deterministic (not random) UUIDs: same input always produces the same UUID, which is what makes re-ingestion idempotent instead of creating duplicate vectors.
- **Session 2 (F1 fix):** Path traversal defense pattern — never trust a caller-supplied path string directly. Join it onto a fixed, trusted base directory, call `.resolve()` (collapses `..` segments and symlinks into a canonical absolute path), then use `Path.is_relative_to(base)` to confirm the result is still inside the allowed directory before touching the filesystem. Checking the *string* for `".."` is not sufficient (e.g. symlinks, encoded separators) — always validate the *resolved* path.
- **Session 2 (F1 fix):** `pytest` fixtures (`tmp_path`) — pytest gives every test function a fresh, auto-cleaned temporary directory via the built-in `tmp_path` fixture, which is why the traversal tests can create real files without touching the actual project directory or leaving artifacts behind.
- **Session 3 (F3 fix):** Fail fast at the source, not at every call site — the check for mismatched/`None` embeddings lives once inside `embed_texts` rather than being copy-pasted into `_upsert` and `_search`. Any caller either gets a fully-valid result or an exception; there's no third state where partially-bad data flows further downstream. Same principle as F1's `resolve_safe_pdf_path`.
- **Session 3 (F4 fix):** `unittest.mock.patch.object` — used to replace `client.models.embed_content` (a real network call) with a fake return value for the duration of a `with` block, so tests exercise `embed_texts`'s own logic without hitting the real Gemini API. `pytest`'s `monkeypatch.setenv`/`monkeypatch.delenv` do the same job for environment variables, automatically restoring the original value after each test.

## 10. FastAPI Concepts Learned

- **Session 3 (F4 fix):** `lifespan` — the current, non-deprecated way to run startup/shutdown code in FastAPI. An `@asynccontextmanager` function takes `app`, runs setup code, then `yield`s (the app serves requests while "paused" on the yield), and anything after the `yield` would run on shutdown. Wired in via `FastAPI(lifespan=lifespan)`. In this project it's used to validate `GEMINI_API_KEY` exists once, before the app accepts any requests, instead of checking (or failing) inside every request that needs it.
- **Session 3 (F4 fix):** `fastapi.testclient.TestClient` used as a context manager (`with TestClient(app): ...`) actually runs the app's `lifespan` startup/shutdown events, which is what makes it possible to unit-test startup-time behavior (like our env-var check) without running a real `uvicorn` server.
- **Session 5 (Step 4):** ASGI vs WSGI — ASGI is the async-capable successor protocol Uvicorn/Starlette/FastAPI speak; it's *why* `async def` route handlers and lifespan hooks work at all. FastAPI = Starlette (ASGI framework: routing, middleware, lifespan) + Pydantic (validation/serialization) + auto-generated OpenAPI docs.
- **Session 5 (Step 4):** A library can register routes on an `app` object you own, without you writing `@app.get(...)` yourself — `inngest.fast_api.serve(app, ...)` calls FastAPI's own decorators on your instance. Reading a dependency's source (`inspect.getsource`) is a legitimate, fast way to find out exactly what routes/behavior it adds instead of guessing from docs.
- **Session 5 (Step 4):** `Depends()` — FastAPI's dependency injection: a route parameter typed as `Depends(some_callable)` gets that callable's return value injected automatically, resolved fresh per request (or cached per-request if the same dependency is needed twice). Not used yet in this project, but `get_storage()` (the F5 fix) is already shaped to become one directly.
- **Session 5 (Step 4):** `lifespan` runs exactly once at process startup/shutdown, not per-request — contrast with `Depends()`, which runs per-request. Different tools for "set up once" vs. "provide fresh/shared state to this specific request."

## 11. RAG Concepts Learned

- **Session 1:** Grounding/citation pattern — the prompt asks the model to both answer *and* self-report which numbered context chunks it used, which is then parsed back into human-readable sources. This is a cheap way to get attribution without a second retrieval-verification pass, but it trusts the model's self-report (it can hallucinate a `Used:` line that doesn't reflect what it actually relied on).
- **Session 1:** Chunk/embedding alignment invariant — `chunks`, `sources`, `ids`, `vecs`, `payloads` are all parallel lists indexed by position. Any step that changes list length without updating all four breaks retrieval silently (see finding F3).
- **Session 5 (Step 4):** Inngest's durable-execution model — a function invocation is replayed via repeated HTTP calls (one per step), not run once end-to-end. Already-completed `ctx.step.run(...)` calls return their memoized result instantly on replay instead of re-executing; only the next incomplete step actually runs. This is *why* steps need to be deterministic/side-effect-safe, and *why* raising a clear exception inside a step (rather than swallowing it) is the correct failure signal — Inngest's retry mechanism is driven entirely by unhandled exceptions. Full detail in §7.3.

## 12. Bugs / Issues Discovered (Session 1 review — prioritized)

| # | Severity | Area | What |
|---|---|---|---|
| F1 | Critical | Security | ✅ Fixed (Session 2) — Unvalidated `pdf_path` from event data |
| F2 | Critical | Correctness | ✅ Verified working (Session 2, user tested against real API with their key) — Gemini model id `gemini-3.6-flash` |
| F3 | High | Correctness | ✅ Fixed (Session 3) — `embed_texts` result not checked for `None`/length mismatch before upsert |
| F4 | High | Reliability | ✅ Fixed (Session 3) — `GEMINI_API_KEY` only validated at call time, not startup |
| F5 | Medium | Performance | ✅ Fixed (Session 8) — singleton client (Session 4) + full `AsyncQdrantClient`/async-genai conversion (Session 8, see D7) |
| F6 | Medium | Testing | ✅ Fixed (Session 11) — grew from zero tests (Session 1) to 49 passing across every module, including endpoint-level and cross-endpoint-wiring coverage for all three REST routes |
| F7 | Low | Correctness | Query function's `fn_id="RAG: Query pdf"` and its trigger event `rag/ingest_pdf_ai` are misleading/inconsistent names (ingest vs query) |
| F8 | Low | UX/Correctness | ✅ Fixed (Session 8) — resolved as a side effect of implementing `POST /ask`'s empty-context short-circuit (`rag_service.answer_question`). Only fixed on the new `/ask` path; the existing Inngest `rag_query_pdf_ai` function is untouched, per "no behavior change" |
| F9 | Low | Maintainability | ✅ Fixed (Session 7) — Business logic (`_load`, `_upsert`, `_search`) is nested inside Inngest function bodies in `main.py` rather than a separate service/module layer |
| F10 | Info | Git hygiene | Repo has no completed baseline commit (see §4) — must be fixed before branching |
| F11 | Low | Config | Discovered Session 5 — `pyproject.toml` console script `fastapiproject:main` doesn't exist (`__init__.py` is empty); the real launch command is `uv run uvicorn fastapiproject.main:app --reload` |

### F1 — Unvalidated `pdf_path` — ✅ FIXED (Session 2)
- **WHAT:** `pdf["pdf_path"]` from event payload is passed directly to `Path()` and read, with no restriction on location.
- **WHY:** Event payloads are external input (anything that can publish to the Inngest endpoint controls this path).
- **IMPACT:** Path traversal / arbitrary local file read (e.g. `../../etc/passwd`-style paths) once ingestion is reachable by less-trusted callers (which it will be once wrapped in a REST endpoint in Step 6).
- **SOLUTION CHOSEN:** Added `resolve_safe_pdf_path(path, base_dir=PDF_UPLOAD_ROOT)` in `data_loader.py`. `PDF_UPLOAD_ROOT` defaults to the project root (`Path(__file__).resolve().parents[2]`), overridable via the `PDF_UPLOAD_ROOT` env var. It joins the candidate path onto `base_dir`, resolves it (`Path.resolve()` collapses `..` and symlinks), and rejects it with `ValueError` unless `candidate.is_relative_to(base_dir)`. Also raises `FileNotFoundError` if the resolved path doesn't exist. `load_and_chunk_pdf` now calls this before handing the path to `PDFReader`.
  - **Design decision (see D3 below):** user's initial preference was for `pdf_path` to eventually come from a Streamlit file-upload widget rather than raw text input. That's the right long-term shape (Step 5/6 concern — an upload endpoint that saves bytes server-side, so there's no attacker-controlled path string at all). This fix hardens the *current* Inngest-event code path today, and `resolve_safe_pdf_path` is exactly the guard that endpoint's save step will reuse — the base-directory-containment check is identical regardless of where the path string originates.
  - Default base dir = project root (not a narrower `data/pdfs/` folder) specifically so the existing manually-tested flow (`pdf_path="test.pdf"`) keeps working unchanged — no breaking change to already-verified behavior.
- **TESTS ADDED:** `tests/test_data_loader.py` (4 cases, all passing): relative path inside root resolves; `..`-traversal outside root raises `ValueError`; absolute path outside root raises `ValueError`; missing-but-in-root file raises `FileNotFoundError`.
- **VERIFICATION PERFORMED:** `uv run pytest -q` → 4 passed. Manually re-ran `load_and_chunk_pdf('test.pdf')` against the real `test.pdf` (with real Gemini API key loaded from `.env`) → succeeded, 1 chunk extracted, confirming the fix doesn't break the previously-working path. Manually confirmed `load_and_chunk_pdf('../../test.pdf')` (escapes project root) is rejected with the expected `ValueError`.

### F2 — Gemini model id — ✅ VERIFIED WORKING (Session 2)
- **WHAT:** `model="gemini-3.6-flash"` in `main.py`.
- **STATUS:** User confirmed they tested this locally against the real Gemini API with their own API key and it works. No code change made — flagged as unverified from static review only; live testing overrides that. Leaving as-is per "if something is already correct, leave it unchanged."

### F3 — Silent embedding misalignment — ✅ FIXED (Session 3)
- **WHAT:** `embed_texts` return type is `list[list[float|int] | None]`; `_upsert` and `_search` never check for `None` or length mismatches before zipping with `ids`/`payloads`/`sources`.
- **WHY:** All downstream code assumes parallel-list alignment (see §11).
- **IMPACT:** A single failed embedding shifts every subsequent id/payload pairing — wrong text gets attached to wrong vector, corrupting retrieval silently (no exception raised).
- **SOLUTION CHOSEN:** `embed_texts` in `data_loader.py` now validates immediately after the Gemini call: raises `RuntimeError` if `len(embeddings) != len(texts)` (count mismatch), and `RuntimeError` if any individual embedding's `.values` is `None`. Return type tightened from `list[list[float|int] | None]` to `list[list[float]]` — callers (`_upsert`, `_search` in `main.py`) needed no changes, since they can now trust the invariant holds or an exception already stopped execution before they run. Validating inside `embed_texts` itself (rather than duplicating the check in every caller) keeps the invariant enforced at its single point of origin — same principle as F1's fix.
- **TESTS ADDED:** `tests/test_embed_texts.py` (3 cases, mocking `client.models.embed_content` so no real API calls are needed): correct-length input returns matching vectors; short embeddings list raises `RuntimeError`; a `None`-valued embedding raises `RuntimeError`.
- **VERIFICATION PERFORMED:** `uv run pytest -q` → all passing. Manually re-ran `embed_texts(['hello world', 'second chunk of text'])` against the real Gemini API → 2 vectors returned, each dim 3072 (matches `EMBED_DIM`), confirming no regression on the successful path.

### F4 — API key validated too late — ✅ FIXED (Session 3)
- **WHAT:** `os.environ["GEMINI_API_KEY"]` is read inside the query function body.
- **WHY:** Fails on first real request instead of at process startup.
- **IMPACT:** Confusing runtime `KeyError` deep in a request instead of a clear boot-time failure.
- **SOLUTION CHOSEN:** Added a FastAPI `lifespan` context manager in `main.py` (`_validate_required_env_vars()` + `@asynccontextmanager async def lifespan(app)`), wired via `FastAPI(lifespan=lifespan)`. It checks `REQUIRED_ENV_VARS = ["GEMINI_API_KEY"]` and raises a clear `RuntimeError` naming the missing variable(s) before the app finishes starting, instead of only failing inside a request handler. `os.environ["GEMINI_API_KEY"]` inside `rag_query_pdf_ai` is left as-is — safe now that startup guarantees the key exists before any request is served.
  - **Why `lifespan` over a simpler module-level constant check:** `data_loader.py`'s `genai.Client()` already fails at import time if the key is entirely missing (the SDK's own check), so a second guard was arguably redundant for that one variable. `lifespan` was chosen anyway because it's the idiomatic, current FastAPI startup-validation pattern (the deprecated alternative is `@app.on_event("startup")`), it's independently testable via `TestClient`, and it gives a single, extensible place to add more required vars later (e.g. `QDRANT_URL`) without relying on import-order side effects.
- **TESTS ADDED:** `tests/test_main_startup.py` (2 cases, using `fastapi.testclient.TestClient` + `monkeypatch` on `os.environ`): app starts cleanly when `GEMINI_API_KEY` is set; app raises `RuntimeError` mentioning `GEMINI_API_KEY` when it's unset, at `TestClient` construction time (i.e. at lifespan startup) rather than during a request.
- **VERIFICATION PERFORMED:** `uv run pytest -q` → all passing (9/9 total across the project at this point).

### F5 — Qdrant client lifecycle
- **WHAT:** `QdrantStorage()` constructed fresh inside every `_upsert`/`_search` call; uses the sync `QdrantClient` inside `async def` Inngest step functions; re-checks `collection_exists` every time.
- **WHY:** Sync network calls inside an async function block the event loop; recreating the client/collection check adds latency for no benefit.
- **IMPACT:** Poor throughput under concurrent requests; unnecessary Qdrant round-trips.
- **SOLUTION CHOSEN (partial — see D4):** Added a module-level singleton in `vector_db.py`: `get_storage()` lazily constructs one `QdrantStorage` (and therefore runs `collection_exists`/`create_collection` exactly once per process) and returns the same instance on every subsequent call. `main.py`'s `_upsert`/`_search` now call `get_storage()` instead of `QdrantStorage()`. This fixes the "recreated per call" and "repeated collection check" parts of F5.
  - **Deferred: switching to `AsyncQdrantClient`** (the "blocks the event loop" part). See Decision D4 — confirmed via reading `inngest`'s own source (`step_async.py`) that non-async step handlers are called directly on the event loop, not offloaded to a thread, so the blocking concern is real. Not fixed this session; reasoning and revisit trigger are in D4.
- **TESTS ADDED:** `tests/test_vector_db.py` (2 cases, mocking the `QdrantStorage` class so no real Qdrant connection is needed): `get_storage()` constructs the class only once across repeated calls; `get_storage()` returns a pre-existing singleton without re-constructing.
- **VERIFICATION PERFORMED:** `uv run pytest -q` → all passing. Manually confirmed `main.py` still imports cleanly with the `get_storage()` swap. **Could not verify end-to-end against a real Qdrant instance this session** — `localhost:6333` was unreachable (Qdrant not running locally at the time). Flagged here rather than silently skipped, per project rule against claiming unverified things work.

### F6 — No tests
- **WHAT / IMPACT / SOLUTION:** No automated tests exist anywhere. Every fix in Step 3 must land with a corresponding test so regressions are caught mechanically, not by re-reading code.
- **VERIFICATION:** `pytest` run showing new tests passing, added to this doc's Testing Notes section per change.

### F7, F8 — still deferred, low severity
Documented for awareness; will be addressed opportunistically alongside related work rather than as standalone fixes, to avoid unnecessary churn on working code (per project constraints). (F8 is expected to get resolved as a side effect of Step 6's `/ask` endpoint — see §8.3 — but not touched in the still-unmodified Inngest `rag_query_pdf_ai` function.)

### F9 — Business logic nested in Inngest function bodies — ✅ FIXED (Session 7)
- **WHAT:** `_load`, `_upsert`, `_search`, plus inline prompt-building and citation-parsing code, lived as closures/inline blocks directly inside `rag_ingest_pdf`/`rag_query_pdf_ai` in `main.py`.
- **WHY:** Step 5's new REST endpoints need this exact logic too; duplicating it inline in a second place would mean every future bug fix has to be applied twice.
- **IMPACT (if left unfixed):** `/ask` and `/ingest` would either duplicate the retrieval/prompt/citation logic (drift risk) or be unable to reuse it cleanly.
- **SOLUTION CHOSEN:** New module `src/fastapiproject/rag_service.py` holds `load_and_chunk_sources`, `embed_and_upsert`, `search_context`, `build_prompt`, `parse_cited_answer`, and the `SYSTEM_INSTRUCTION` constant — exactly the functions specified in the Step 5 design (§8.2). `main.py`'s `_load`/`_upsert`/`_search` closures are now thin wrappers that just call into `rag_service`; `rag_query_pdf_ai` calls `rag_service.build_prompt(...)` and `rag_service.parse_cited_answer(...)` instead of the old inline code. **Pure extraction — no behavior change** to the two Inngest functions.
  - One small, deliberate addition needed to make the extraction clean: a new `PdfRef` Pydantic model (`custom_types.py`) replaces raw `dict` access for PDF references, since `load_and_chunk_sources` needed a real type to accept instead of `list[dict]`. `_load` now does `[PdfRef(**pdf) for pdf in ctx.event.data["pdfs"]]` before calling the service function — malformed event data now raises a clear Pydantic validation error instead of a raw `KeyError`, a minor incidental improvement, not the point of this change.
  - **Verified behavior parity for the `source_id` default:** original code used `pdf.get("source_id", pdf_path)` (dict `.get` with a default). The Pydantic equivalent can't perfectly distinguish "key missing" from "key explicitly `null`" (both become `None` after parsing) — `load_and_chunk_sources` uses `pdf.source_id if pdf.source_id is not None else pdf.pdf_path`, which matches original behavior for every realistic input (missing key, or a real string value) and only diverges in the never-actually-used case of an event explicitly sending `"source_id": null`. Documented here rather than silently claimed identical.
- **TESTS ADDED:** `tests/test_rag_service.py` (9 cases, all mocked — no real Gemini/Qdrant calls): `load_and_chunk_sources` (source_id defaulting, explicit source_id, multi-PDF combination), `embed_and_upsert` (deterministic ids, correct payload shape), `search_context` (correct args passed through, result shape), `build_prompt` (context numbering, question inclusion), `parse_cited_answer` (citation extraction, empty `Used: []`, fallback when no citation line present).
- **VERIFICATION PERFORMED:** `uv run pytest -q` → 20/20 passed (full project total). Manually confirmed `main.py` still imports cleanly, and re-ran `rag_service.load_and_chunk_sources` against the real `test.pdf` end-to-end (same chunk/source output as before the extraction).

### F10 — Git baseline
- **SOLUTION:** First commit on `main` will be "baseline: working RAG pipeline as-is" containing the currently-working code untouched, before any fix/feature branch is cut. This gives every subsequent PR a clean diff against real history.

### F11 — Broken console script entry point (discovered Session 5, Step 4)
- **WHAT:** `pyproject.toml` declares `[project.scripts] fastapiproject = "fastapiproject:main"`, but `src/fastapiproject/__init__.py` is empty — there is no `main` callable in the package.
- **WHY:** Nothing wires the declared entry point to anything; it was likely scaffolded by `uv init`/`uv build` templates and never filled in.
- **IMPACT:** Low — doesn't affect the app when run the normal way (`uv run uvicorn fastapiproject.main:app --reload`). But `uv run fastapiproject` (the "obvious" thing to try, especially since `.venv/Scripts/fastapiproject.exe` exists and looks like a real entry point) fails immediately, which is confusing for anyone new to the repo — including, eventually, whoever reads this for a portfolio/resume review.
- **SOLUTION (not yet applied):** Either (a) add a `main()` function in `__init__.py` that calls `uvicorn.run("fastapiproject.main:app", ...)`, making `uv run fastapiproject` a real, working shortcut, or (b) remove the `[project.scripts]` entry entirely if a single documented `uvicorn` command is preferred. Deferred — low severity, doesn't block Step 4/5.
- **VERIFICATION (of the finding only):** `python -c "import fastapiproject; print(hasattr(fastapiproject, 'main'))"` → `False`, confirming the entry point is broken as declared.

## 13. Bugs Fixed

| # | Fixed in | Summary |
|---|---|---|
| F1 | Session 2, branch `fix/rag-pipeline-hardening` | Added `resolve_safe_pdf_path()` path-containment check before any PDF is read; see full writeup under F1 in §12. |
| F3 | Session 3, branch `fix/rag-pipeline-hardening` | `embed_texts` now validates embedding count and non-`None` values, raising `RuntimeError` instead of silently misaligning ids/vectors/payloads; see F3 in §12. |
| F4 | Session 3, branch `fix/rag-pipeline-hardening` | Added FastAPI `lifespan` startup check for `GEMINI_API_KEY`; app now fails fast at boot with a clear message instead of a buried `KeyError` mid-request; see F4 in §12. |
| F5 (partial) | Session 4, branch `fix/rag-pipeline-hardening` | Added `get_storage()` singleton in `vector_db.py` — one `QdrantClient` and one `collection_exists` check per process instead of per call. Async-client conversion deferred, see D4. |
| F5 (full) | Session 8, branch `feature/ask-endpoint` | Converted `vector_db.QdrantStorage` to `AsyncQdrantClient`, `data_loader.embed_texts` to `client.aio.models.embed_content`, and `rag_service`'s `embed_and_upsert`/`search_context`/`generate_answer`/`answer_question` to `async def`. See F5 in §12 and Decision D7. |
| F8 | Session 8, branch `feature/ask-endpoint` | `rag_service.answer_question` short-circuits with `NO_CONTEXT_ANSWER` when `search_context` returns no contexts, instead of sending an empty context block to Gemini. Only applies to the new `/ask` path. See F8 in §12. |
| F9 | Session 7, branch `feature/rag-service-extraction` | Extracted `_load`/`_upsert`/`_search`/prompt-building/citation-parsing into new `rag_service.py`; `main.py`'s Inngest functions now call into it, no behavior change. See F9 in §12. |
| F6 | Session 11, branch `feature/api-tests` | Added remaining endpoint edge-case coverage (`/ask` `score_threshold` bounds + unhandled-error 500; `/ingest` multi-file upload, missing-filename rejection, Inngest-send-failure 500) plus `tests/test_full_flow.py` — two cross-endpoint tests that exercise `/ask`'s full real call chain (router → `rag_service` → `vector_db`/`data_loader`) with only the true external clients (Qdrant, Gemini) mocked, unlike the per-endpoint tests which stub `rag_service.answer_question` directly. All new cases passed against the already-correct implementation — this closed a coverage gap, not a behavior bug. See F6 in §12. |

## 14. Features Added

| Feature | Added in | Summary |
|---|---|---|
| `POST /ask` | Session 8, branch `feature/ask-endpoint` | Synchronous REST endpoint per the §8.3 design: `AskRequest` (validated `question`/`top_k`/`score_threshold`), calls `rag_service.answer_question` (retrieval → empty-context short-circuit (F8) → prompt → direct Gemini call (Decision D5) → citation parsing), returns `QueryResult`. Registered via a new `src/fastapiproject/routers.py` `APIRouter` (§8.5), included into `app` in `main.py`. Maps Qdrant connection failures to `503`; validation failures are automatic `422`s from Pydantic. |
| `POST /ingest` | Session 9, branch `feature/ingest-endpoint` | Multipart file upload per §8.3/D6: validates non-empty file list, `.pdf` extension + `application/pdf` content-type (`422` on either), ≤20MB per file (`413`). Each file's name is sanitized to its basename (`Path(file.filename).name`) and prefixed with `uuid4().hex` before saving under `PDF_UPLOAD_ROOT/uploads/`; the saved path is re-validated through F1's `resolve_safe_pdf_path` before being used (defense in depth). Sends the same `rag/ingest_pdf` event the existing Inngest function already consumes — `rag_ingest_pdf` itself is untouched. Returns `202` with `{event_id, status_url}`. |
| `GET /ingest/{event_id}/status` | Session 9, branch `feature/ingest-endpoint` | Calls Inngest's own REST API (`GET {api_origin}v1/events/{event_id}/runs`, confirmed via Context7 in Session 6) using `httpx`. No-runs-yet → `200` `status: "Queued"` (not `404`) since a client polling immediately after `POST /ingest` may beat Inngest to creating the run. `Completed` → `ingested` populated from the run's `output`; `Failed` → `error` populated. Connection-level failures (`httpx.RequestError`) → `503`; a non-2xx response *from* Inngest → `502` (Decision D8 distinguishes these two cases, matching §8.4's status table). **The exact `output`/`error` field names on the run object are inferred from Inngest's docs/examples, not confirmed against a live response** — see D8 and §24. |
| Streamlit frontend (`api_client.py` + `streamlit_app.py`) | Session 12, branch `feature/streamlit-frontend` | The project's first UI, built in an isolated worktree via subagent-driven-development. New `src/fastapiproject/api_client.py`: three thin synchronous `httpx` wrappers over the REST endpoints (§8.3) — `ask`, `ingest`, `check_status` — sharing a common `_request`/`ApiError` foundation. New `src/fastapiproject/streamlit_app.py`: two tabs, "Ask a Question" and "Upload PDFs". `BACKEND_URL` env var (default `http://127.0.0.1:8000`) decouples the frontend from a hardcoded backend location, enabling separate deployment later without a code change. Built with a deliberate visual design system rather than default Streamlit styling — sage-gray/white/teal-pine/brick-red palette, Source Serif 4 + IBM Plex Sans/Mono typefaces, responsive max-width layout, and a citation-chip component as the signature visual element for cited sources; full rationale in `docs/superpowers/specs/2026-08-17-streamlit-frontend-design.md` §4.3. Run as two separate processes: `uv run uvicorn fastapiproject.main:app --reload` (backend) + `uv run streamlit run src/fastapiproject/streamlit_app.py` (frontend). |

## 15. Tests

**Test infra:** `pytest` added as a dev dependency via `uv add --dev pytest` (Session 2). Run with `uv run pytest`.

| File | Covers | Cases | Result |
|---|---|---|---|
| `tests/test_data_loader.py` | `resolve_safe_pdf_path` (F1 fix) | relative path inside root resolves; `..` traversal outside root rejected (`ValueError`); absolute path outside root rejected (`ValueError`); missing file inside root raises `FileNotFoundError` | 4/4 passed |
| `tests/test_embed_texts.py` | `embed_texts` (F3 fix; F5 async conversion), mocked `client.aio.models.embed_content` | correct-length response returns matching vectors; short response raises `RuntimeError`; `None`-valued embedding raises `RuntimeError` (all async since Session 8) | 3/3 passed |
| `tests/test_main_startup.py` | `lifespan` startup validation (F4 fix), via `TestClient` + `monkeypatch` | app starts with `GEMINI_API_KEY` set; app raises `RuntimeError` at startup when it's unset | 2/2 passed |
| `tests/test_rag_service.py` | `rag_service.py` (F9 extraction; F5/F8/`/ask` additions), fully mocked (no real Gemini/Qdrant calls) | `load_and_chunk_sources` source_id defaulting/explicit/multi-PDF; `embed_and_upsert` deterministic ids + payload shape (async); `search_context` args/result shape (async); `build_prompt` numbering/question; `parse_cited_answer` extraction/empty-list/no-citation-line fallback; `generate_answer` calls Gemini via `client.aio.models.generate_content` and strips the response (async); `answer_question` short-circuits on empty context (F8) / builds prompt + parses citation on real context (async) | 12/12 passed |
| `tests/test_vector_db.py` | `QdrantStorage`/`get_storage()`, mocked `AsyncQdrantClient` | singleton construct-once/reuse (unchanged); `upsert` creates collection when missing / skips when present / only checks existence once across calls; `search` returns context+sources from matched points | 6/6 passed |
| `tests/test_ask_endpoint.py` | `POST /ask` end-to-end via `TestClient`, `rag_service.answer_question` mocked (auto-`AsyncMock`'d since the target is `async def`) | 200 with answer; custom `top_k`/`score_threshold` passed through; empty `question` → 422; `top_k`/`score_threshold` out of bounds → 422; Qdrant `ApiException` → 503; unhandled error → 500 (via `TestClient(..., raise_server_exceptions=False)`) | 7/7 passed |
| `tests/test_ingest_endpoint.py` | `POST /ingest` + `GET /ingest/{event_id}/status` end-to-end via `TestClient`; `inngest_client.send` and `httpx.AsyncClient.get` mocked, `PDF_UPLOAD_ROOT` monkeypatched to `tmp_path` | empty file list → 422; wrong content-type → 422; oversized file → 413; valid upload saves bytes to disk + sends the correct event + returns 202 with `event_id`/`status_url`; path-traversal filename sanitized to its basename; multiple files in one request all saved+sent; missing filename → 422; Inngest send failure → 500; status: no-runs → Queued/200; Completed → `ingested`; Failed → `error`; connection failure → 503; upstream error response → 502 | 13/13 passed |
| `tests/test_full_flow.py` | `POST /ask`'s full real call chain (router → `rag_service` → `vector_db`/`data_loader`), only Qdrant's `AsyncQdrantClient` and Gemini's `client.aio.models` mocked — unlike `test_ask_endpoint.py`, which stubs `rag_service.answer_question` itself | full chain returns a grounded, cited answer; full chain reaches F8's empty-context short-circuit without ever calling Gemini's generation endpoint | 2/2 passed |

**Running total:** `uv run pytest -q` → 49/49 passed (Session 11).

**Manual verification (Session 2):** `load_and_chunk_pdf('test.pdf')` re-run against the real sample file with the real Gemini API key loaded — succeeded (1 chunk extracted), confirming F1's fix didn't regress the already-working ingestion path. `load_and_chunk_pdf('../../test.pdf')` confirmed rejected.

**Manual verification (Session 3):** `embed_texts(['hello world', 'second chunk of text'])` re-run against the real Gemini API — returned 2 vectors, dim 3072 each, confirming F3's fix doesn't regress the successful embedding path.

**Manual verification (Session 7):** `main.py` still imports cleanly after the `rag_service` swap; `rag_service.load_and_chunk_sources([PdfRef(pdf_path='test.pdf')])` re-run end-to-end against the real `test.pdf` — same chunk/source output as before the extraction.

**Manual verification (Session 4):** Confirmed `main.py` still imports cleanly after swapping `QdrantStorage()` calls for `get_storage()`. Could **not** verify end-to-end against a real Qdrant instance — `localhost:6333` was unreachable this session (Qdrant not running). Recommend re-running `_upsert`/`_search` manually once Qdrant is up, before relying on this in a demo.

## 16. Known Limitations (current, as-is)

- All three §8.3 endpoints are implemented (`POST /ask` Session 8; `POST /ingest`/`GET /ingest/{event_id}/status` Session 9). The status endpoint's run-object field parsing (`output`/`error`) is unverified against a live Inngest server — see D8/§24.
- No auth/rate limiting (still true after Step 5's design — not in scope; would be a Step 7+ concern).
- No upload size limits on the *existing* Inngest-event ingestion path (Step 5's `/ingest` design closes this for the new endpoint specifically — see §8.3).
- Streamlit frontend now exists (`api_client.py` + `streamlit_app.py`, Session 12) — the previously-unused Streamlit dependency is in use; see §14.
- README.md is empty.
- `uv run fastapiproject` console script is broken (F11) — use `uv run uvicorn fastapiproject.main:app --reload` instead.
- No incoming validation on Inngest event payloads (raw dict access; see §7.5) — still true for the event-driven path; `/ask`'s new `AskRequest` model closes this gap only for requests coming through REST.
- See §12 for the full findings list.

## 17. Design Decisions

### D1 — Documentation source format
**Question:** `FastAPIProject_Overview.pdf` doesn't exist and PDFs can't be edited directly — what should the living doc's editable source be?
**Options:** (A) Markdown source, rendered to PDF on request. (B) Maintain PDF directly via a generation script each time, no tracked readable source. (C) User supplies an existing PDF as baseline.
**Decision:** A — this file (`FastAPIProject_Overview.md`).
**Reason:** Git-diffable, easy to edit incrementally per the "living document" requirement; PDF can be exported from it whenever a PDF artifact is actually needed.
**Trade-off:** An extra export step is needed if a literal `.pdf` file is required at some point (e.g. to share outside the repo).

### D2 — GitHub repo creation timing
**Question:** `gh` is authenticated and repo-creation-capable, no remote exists yet — create the GitHub repo now or after Steps 1–6?
**Decision:** After Steps 1–6, per the strict workflow order in the project instructions.
**Reason:** Matches explicit instruction to fix existing problems and design/implement the REST API before Step 7 (GitHub).
**Trade-off:** Baseline commit (F10) still needs to happen locally in git now, independent of when the GitHub *remote* is created — local git history and remote hosting are separate concerns.

### D3 — F1 fix scope: validate now vs. wait for a Streamlit upload flow
**Question:** Should F1's fix be a base-directory path-containment check on the current Inngest-event code path, or should we first build the Streamlit upload UI the user has in mind (where the path comes from an actual file upload, not free-text)?
**Options:** (A) Add path-containment validation now, in the existing `data_loader.py`, defaulting to the project root as the allowed base directory. (B) Defer any fix until the Streamlit upload endpoint exists (Step 5/6), since that will change how `pdf_path` is sourced entirely.
**Decision:** A now; B is still coming later and will reuse the same guard.
**Reason:** The vulnerable code path (`rag_ingest_pdf` reading `ctx.event.data["pdfs"][i]["pdf_path"]`) is live *today*, independent of whether a Streamlit UI exists yet — it needs to not be exploitable regardless of what future UI calls it. Building the Streamlit upload flow is a Step 5/6-sized task (needs a REST endpoint, file handling, etc.) and out of order per the user's strict workflow. `resolve_safe_pdf_path()` is written so it's exactly the function an upload-endpoint's save step will call too — no throwaway work.
**Trade-off:** None significant — this is additive; nothing about it needs to change when the Streamlit flow is built later.

### D4 — Defer `AsyncQdrantClient` conversion (F5's remaining scope)
**Question:** F5 flagged that sync Qdrant calls inside `async def` Inngest step handlers block the event loop. Fix it now by converting to `AsyncQdrantClient` (and making `_upsert`/`_search` async), or defer?
**Investigation:** Read `inngest`'s own SDK source (`.venv/.../inngest/_internal/step_lib/step_async.py`, `Step.run`): confirmed non-async handlers passed to `ctx.step.run(...)` are called directly (`handler(*handler_args)`) on the running event loop, not offloaded to a thread pool — so the blocking concern in F5 is real, not theoretical.
**Decision:** Defer the async conversion. Fix only the singleton/one-time-check part of F5 this session.
**Reason:** The blocking currently has near-zero practical impact — there is no REST API yet (Step 5/6), so nothing sends concurrent user-facing HTTP requests into this process; Inngest itself processes one step at a time in this flow. Converting now means changing `_upsert`/`_search` to `async def`, switching to `AsyncQdrantClient`, and reasoning about how `asyncio.to_thread`/async calls interact with Inngest's step memoization and retry semantics — a nontrivial change for a problem that isn't causing harm yet, and `_load`/`_upsert`/`_search` are slated to move into a proper service layer in Step 5 anyway (F9), which is a more natural place to redesign this.
**Trade-off:** If Step 6's REST API ends up handling concurrent requests through this same code path before that refactor happens, the blocking becomes real and should be revisited immediately — noted in §24 Remaining Technical Debt as a trigger condition, not just a someday-maybe.

### D5 — `POST /ask` gets its own Gemini call, not shared with the Inngest function
**Question:** `rag_query_pdf_ai` generates its answer via `ctx.step.ai.infer(...)` — Inngest's own wrapper that makes the LLM call a durable, retryable, observable step. `/ask` is synchronous and has no Inngest step context to call that from. Share the generation code path anyway, or let it diverge?
**Options:** (A) Give `/ask` its own direct call to Gemini via the plain `google-genai` client (same client already used for embeddings in `data_loader.py`), duplicating a small amount of "call the model" code. (B) Force `/ask` to go through Inngest too (contradicts the chosen mixed-model design — user already rejected the uniform-async option). (C) Rewrite `rag_query_pdf_ai` to also call Gemini directly, dropping `ctx.step.ai.infer`, so both paths share one implementation.
**Decision:** A.
**Reason:** (B) was already ruled out by the API-shape decision. (C) would remove Inngest's retry/observability from the *event-driven* query path for no benefit and violates "don't unnecessarily rewrite working code." (A) keeps both paths working as designed, and everything *except* the actual model call — retrieval (`search_context`), prompt construction (`build_prompt`), citation parsing (`parse_cited_answer`) — is still fully shared via `rag_service.py`. Only the ~5-10 lines that actually invoke Gemini differ.
**Trade-off:** Two places call Gemini for generation instead of one. Acceptable because the two call sites have genuinely different requirements (durable step vs. synchronous call) — this isn't accidental duplication, it's two different integrations with the same model.

### D6 — `/ingest` takes real file uploads, not a path reference
**Question:** Should `POST /ingest` accept a JSON body referencing a path that already exists on the server (mirroring today's Inngest event schema exactly), or accept actual uploaded file bytes (`multipart/form-data`)?
**Decision:** Real file upload (`UploadFile`).
**Reason:** Follows directly from Decision D3 (Session 2) — the user's stated direction was for `pdf_path` to eventually come from an upload flow, not typed/referenced text. Since Step 5 is a from-scratch design (no existing REST behavior to preserve), there's no reason to design the worse intermediate version first. `resolve_safe_pdf_path` (F1) still does exactly the same job here — validating the path *after* our own code generates it, as defense in depth.
**Trade-off:** More implementation work in Step 6 (multipart handling, filename sanitization, size limits) than a JSON-body version would need — accepted, since it's the design that's actually useful for a live demo/portfolio piece.

### D7 — Convert to `AsyncQdrantClient`/async Gemini calls now, inside `feature/ask-endpoint` (resolves F5 fully, supersedes D4)
**Question:** D4 deferred the `AsyncQdrantClient` conversion with an explicit trigger: revisit immediately once a REST endpoint sends concurrent traffic through `search_context`/`embed_and_upsert`. `POST /ask` (Session 8) does exactly that — do the conversion now, or keep deferring?
**Refinement discovered this session:** `routers.ask` is a **sync** `def` path operation (`def ask(...)`, not `async def`). FastAPI/Starlette runs sync path operations in a threadpool automatically, not on the main event loop — this is a materially different situation from D4's original finding (Inngest's `ctx.step.run` calls sync handlers *directly on the event loop*, confirmed by reading `step_async.py` in Session 4). So the specific failure mode D4 warned about (stalling the whole event loop, blocking every other in-flight request) does not reproduce for `/ask` the way it would have for an Inngest step. The remaining risk is milder: threadpool exhaustion (Starlette's default pool is ~40 workers) under heavy concurrent `/ask` load, causing queued requests/latency rather than a full stall.
**Options presented:** (A) Defer — log the refined trigger condition, do the conversion as its own future branch. (B) Convert now — `AsyncQdrantClient`, `client.aio.models.embed_content`/`generate_content`, `async def` throughout `rag_service.py`, inside this branch.
**Decision:** B (user's explicit choice after reviewing the pros/cons, overriding the assistant's recommendation of A).
**What changed:**
- `vector_db.QdrantStorage`: `QdrantClient` → `AsyncQdrantClient`. `collection_exists`/`create_collection` can't run in `__init__` anymore (async), so they moved into a new `_ensure_collection()` method, called lazily on the first `upsert()`/`search()` and cached via a `_collection_ready` flag — same "once per process" behavior as before, just deferred to first use instead of construction. `get_storage()` itself is **unchanged and still sync** — building an `AsyncQdrantClient` object does no I/O, only calling its methods does.
- `data_loader.embed_texts`: → `async def`, uses `client.aio.models.embed_content` (confirmed via Context7 docs — same `genai.Client()` instance exposes both `.models` (sync) and `.aio.models` (async), no separate client needed).
- `rag_service.py`: `embed_and_upsert`, `search_context`, `generate_answer`, `answer_question` all → `async def`, awaiting their now-async dependencies. `generate_answer` uses `client.aio.models.generate_content` (also confirmed via Context7).
- `main.py`: `rag_ingest_pdf`'s `_upsert` and `rag_query_pdf_ai`'s `_search` closures → `async def`, `await`ing into `rag_service`. **No change needed to the `ctx.step.run(lambda: _upsert(...), ...)` call sites** — confirmed by reading `step_async.py`/`transforms.maybe_await`: Inngest's `ctx.step.run` already detects when a sync-looking handler call returns a coroutine and awaits it (`transforms.maybe_await`), so the lambda-wrapping pattern keeps working unchanged. `_load` stays sync (PDF parsing has no async-capable dependency).
- `routers.ask`: → `async def`, `await`s `rag_service.answer_question`.
- Added `pytest-asyncio` as a dev dependency + `asyncio_mode = "auto"` in `pyproject.toml` — required for `async def test_...` functions to actually execute their bodies (verified this was broken before adding it: a bare `async def test_x(): raise AssertionError(...)` silently "passed" with only a `RuntimeWarning`, since nothing awaited the test coroutine).
**Trade-off:** This is a wider-than-originally-scoped change for a branch whose stated purpose (§19) was "implement `POST /ask`" — it touches `vector_db.py`, `data_loader.py`, `rag_service.py`, and `main.py`'s existing (working, previously "no behavior change") Inngest closures. Accepted as the user's explicit call after reviewing the cost/benefit table. **Could not be verified end-to-end against real Qdrant or a real Gemini API key this session** — `localhost:6333` unreachable and no `GEMINI_API_KEY` set in this session's environment (same class of limitation as Session 4's Qdrant check). All behavior is verified via mocked tests only (32/32 passing); a live check is recommended before relying on this in a demo.

### D8 — `inngest_client` extracted to its own module; `502` vs `503` split for `GET /ingest/{event_id}/status`
**Question 1:** `routers.py` needs the `Inngest` client both to send the `rag/ingest_pdf` event (`POST /ingest`) and to build the status-check URL (`GET /ingest/{event_id}/status`). It previously lived only in `main.py`, which already imports `routers.py` to register `ask_router` — importing it back from `main.py` would be circular.
**Decision:** Moved the `inngest.Inngest(...)` construction into a new `src/fastapiproject/inngest_client.py`; both `main.py` and `routers.py` now import `client` from there. `main.py`'s `inngest_client.create_function(...)` decorator usage and `inngest.fast_api.serve(app, inngest_client, ...)` call are otherwise unchanged (still referenced as `inngest_client` locally via `from fastapiproject.inngest_client import client as inngest_client`).
**Question 2:** §8.3's error table lists both `502` and `503` for `GET /ingest/{event_id}/status` without specifying which failure maps to which.
**Decision:** `503` for connection-level failures (`httpx.RequestError` — timeout, connection refused, DNS failure: Inngest's API isn't reachable at all); `502` for a response that *does* come back from Inngest but with a non-2xx status (Inngest itself reported an error). This matches §8.3's own reasoning ("distinguishes 'your ingestion failed' from 'we can't currently check'") by further splitting "can't check" into "couldn't even connect" (503) vs. "connected, but Inngest said no" (502).
**Caveat (not a decision, a real gap):** the exact JSON field names inside each run object for `Completed`'s output and `Failed`'s error (`run.get("output")`/`run.get("error")` in `routers.ingest_status`) are **inferred from Inngest's documentation/example code, not confirmed against a live response** — no local Inngest Dev Server was reachable in Session 9 to verify. `run.get("status")` is confirmed (used directly in Inngest's own documented polling example). See §24 for the live-verification follow-up this implies.

## 18. Questions & Decisions

Q&A log, chronological:

1. **Q:** Doc source format (see D1). **A:** Markdown.
2. **Q:** GitHub repo creation timing (see D2). **A:** Follow workflow order (hold off).
3. **Q:** F1 fix — which base directory should `pdf_path` be restricted to? **A (user):** "Let the option to upload be coming from the user via the streamlit UI, there we get the path which needs to be filled in" — i.e., long-term the path should come from an upload widget, not typed text. Interpreted as informing the *eventual* design (Step 5/6), while the *immediate* fix (this session) hardens the current Inngest code path with project-root containment — see D3.
4. **Q:** How should the new REST endpoints relate to the existing Inngest functions — sync, async, or a mix? **A (user):** Mixed: sync `/ask`, async `/ingest` (the recommended option). Drove the entire Step 5 design — see §8 and Decisions D5/D6.
5. **Q:** User asked to avoid D5's "direct call the model," concerned it might skip retrieval and break grounding. **Clarified:** retrieval (`search_context`) and grounded prompt construction (`build_prompt`) still run first in `/ask`, unchanged from the Inngest path — "direct call" only refers to which client makes the final network call to Gemini (`google-genai` directly vs. `ctx.step.ai.infer`), not whether retrieved context is used. **A (user):** confirmed this was a misreading, no design change needed. D5 stands as designed.

## 19. Git Branching Strategy

**Updated Session 11 — the full planned branch stack is now done.** All branches below are local-only — no GitHub remote exists yet (Decision D2); Step 7 (GitHub) is the natural next milestone.

```
main
 └── fix/rag-pipeline-hardening          (done: F1, F3, F4, F5-partial, Step 4/5 docs — 6 commits)
      └── feature/rag-service-extraction (done: rag_service.py — F9, no behavior change — 1 commit)
           └── feature/ask-endpoint      (done: POST /ask, F5-full async conversion (D7), F8 — 7 commits)
                └── feature/ingest-endpoint (done: POST /ingest + status, D8 — 4 commits + 1 live-verification docs commit)
                     └── feature/api-tests   (done: F6, remaining edge-case + cross-endpoint coverage — this session)
```

`feature/rag-service-extraction` is the base of the stack (not `feature/fastapi-foundation` as originally sketched in Session 1 — that name predated the actual design; the real first step is the extraction, since both new endpoints and the untouched Inngest functions depend on it).

## 20. Stacked PR History

*(none yet)*

## 21. GitHub Repository Information

- **Status:** Not created yet (see D2). `gh` CLI confirmed authenticated as `Shreyas-Kumar-S` with `repo` scope, SSH protocol, at Session 1 start.
- **Planned name:** `intelligent-doc-answerer` (fallback if taken: TBD with user).

## 22. Session Changelog

### Session 1 — 2026-08-13
- Read full existing codebase (`main.py`, `data_loader.py`, `vector_db.py`, `custom_types.py`, `pyproject.toml`, `.gitignore`).
- Confirmed no `FastAPIProject_Overview.pdf` or equivalent source exists anywhere in the repo or working tree.
- Established this file as the living doc (Decision D1).
- Confirmed `gh` auth state; decided to defer GitHub repo creation (Decision D2).
- Completed Step 1 (understand codebase) and Step 2 (prioritized findings, F1–F10) — see §5, §6, §12.
- Discovered git baseline issue (F10): no completed initial commit exists; three files are staged empty with real content unstaged, several files fully untracked.
- **Resolved F10:** Added `.idea/` and `qdrant_storage/` (497MB local Qdrant data) to `.gitignore` — neither is source and both were about to be committed accidentally. Added `.gitattributes` (`*.pdf binary`) after noticing `core.autocrlf=true` flagged `test.pdf` for LF→CRLF conversion, which would silently corrupt the binary PDF on a future checkout. Committed the working baseline as `deb5a33` on branch `main` (renamed from `master` — no remote existed yet, zero-risk rename — to match the GitHub default and this doc's branching diagram), authored by `Shreyas-Kumar-S`, no co-author trailer, per explicit request.
- **Next:** Step 3 — fix findings in priority order starting with F1/F2, each on its own branch with tests.

### Session 2 — 2026-08-13
- User confirmed F2 (Gemini model id) tested working against the real API with their own key — marked verified, no code change (see F2 in §12).
- Branched `fix/rag-pipeline-hardening` off `main`.
- Asked about F1's allowed-base-directory choice (§18 Q3); user's answer pointed at a future Streamlit-upload-driven design — recorded as Decision D3: fix the current Inngest code path now with project-root containment, keep the same guard function for the future upload endpoint.
- **Fixed F1:** added `resolve_safe_pdf_path()` + `PDF_UPLOAD_ROOT` to `data_loader.py`; `load_and_chunk_pdf` now validates before reading. Full detail in §12 F1, tests in §15.
- Added `pytest` as a dev dependency (`uv add --dev pytest`); added `tests/test_data_loader.py` (4 cases).
- **Tests run:** `uv run pytest -q` → 4/4 passed. Manually re-verified `load_and_chunk_pdf('test.pdf')` still succeeds with the real API key, and that a `..`-escaping path is now rejected.
- Committed F1's fix as `1a912ff` on `fix/rag-pipeline-hardening`.
- **Next:** F3 (embedding alignment), then F4 (startup env validation), per user's explicit ordering.

### Session 3 — 2026-08-13
- **Fixed F3:** `embed_texts` in `data_loader.py` now validates embedding count and non-`None` values, raising `RuntimeError` on mismatch instead of silently misaligning downstream ids/vectors/payloads. Added `tests/test_embed_texts.py` (3 cases, mocked API). `uv run pytest -q` passing; manually re-verified against the real Gemini API (2 inputs → 2 vectors, dim 3072). Committed as `f5d7b6a`.
- **Fixed F4:** Added a FastAPI `lifespan` context manager in `main.py` that validates `GEMINI_API_KEY` is set before the app finishes starting, failing fast with a clear `RuntimeError` instead of a `KeyError` buried inside a request. Added `tests/test_main_startup.py` (2 cases, using `TestClient` + `monkeypatch`). Full rationale (including why `lifespan` was chosen over a simpler module-level check) is under F4 in §12.
- **Tests run:** `uv run pytest -q` → 9/9 passed (full project total).
- F1, F2, F3, F4 are now all resolved — see §12/§13. Remaining open findings: F5 (Qdrant client lifecycle), F6 (broader test coverage), F7–F9 (low severity, deferred).
- **Next:** F5 (Qdrant client lifecycle/perf), or proceed to Step 4 (explain current FastAPI implementation) — user's call.

### Session 4 — 2026-08-13
- User chose F5 next.
- Read `inngest`'s SDK source directly to check whether `ctx.step.run` offloads sync handlers to a thread — confirmed it does not (`step_async.py`), so F5's "blocks the event loop" concern is real, not theoretical.
- **Fixed F5 (partial):** added `get_storage()` module-level singleton in `vector_db.py`; `main.py`'s `_upsert`/`_search` now call it instead of constructing `QdrantStorage()` per call. This gives one `QdrantClient` and one `collection_exists` check per process.
- **Deferred the `AsyncQdrantClient` conversion** — recorded as Decision D4, with an explicit trigger condition (revisit immediately once Step 6's REST API sends concurrent traffic through this code path) rather than an open-ended "later."
- Added `tests/test_vector_db.py` (2 cases, mocked `QdrantStorage`).
- **Tests run:** `uv run pytest -q` → 11/11 passed. Manually confirmed `main.py` still imports cleanly.
- **Could not fully verify:** local Qdrant (`localhost:6333`) was not running this session, so no live end-to-end upsert/search check was possible — noted honestly rather than assumed. Recommend a manual check once Qdrant is running again.
- **Next:** user's call — remaining low-severity findings (F7–F9), broaden test coverage (F6), or move to Step 4 (explain current FastAPI implementation) ahead of REST API design.

### Session 5 — 2026-08-13
- User chose Step 4: explain the current FastAPI/Inngest implementation before designing the REST API.
- Wrote §7 (FastAPI Architecture) covering: the `app` object/ASGI/Uvicorn; how `inngest.fast_api.serve` registers `GET`/`POST`/`PUT /api/inngest` on our `app` (confirmed by reading its source, not assumed); the full request lifecycle including Inngest's step-by-step replay/memoization model and why it makes exception-raising the correct failure signal; Pydantic models' current role (step I/O serialization, not HTTP schemas) vs. their Step 5 future role; the missing-validation gap on incoming event payloads; `Depends()` (unused today, natural fit for `get_storage()` later); and why response/error/status-code handling has nothing to teach yet in this codebase specifically.
- **Discovered F11** while confirming the "how do you actually run this app" claim: `pyproject.toml`'s `fastapiproject:main` console script is broken (`__init__.py` is empty). Verified via `hasattr(fastapiproject, 'main')` → `False`. Logged, not fixed (low severity, doesn't block Step 5).
- Added FastAPI/RAG concept notes to §10/§11 (ASGI vs WSGI, route registration via a third-party library, `Depends()`, `lifespan` vs per-request DI, Inngest's durable-execution/replay model).
- No code changes this session — Step 4 is explanation-only, per the workflow.
- **Next:** Step 5 — design the REST API (endpoints, request/response schemas, validation, status codes) building on §7's findings, before implementing anything.

### Session 6 — 2026-08-13
- Asked the user to resolve the core architectural fork before designing anything: should new REST endpoints be synchronous, fully async via Inngest, or bypass Inngest entirely? User chose the mixed model (sync `/ask`, async `/ingest`) — recorded in §18 Q4.
- Checked Inngest's actual Python SDK surface (`inngest.Inngest.send` signature) and confirmed there's no built-in "get run status" method — a status endpoint has to call Inngest's own REST API directly.
- Looked up Inngest's REST API docs (via Context7, not assumed) for the correct endpoint shape: `GET {origin}/v1/events/{event_id}/runs` (`events/ListEventRuns`), used to design `GET /ingest/{event_id}/status` accurately.
- Wrote the full Step 5 design into §8: three endpoints (`POST /ingest`, `GET /ingest/{event_id}/status`, `POST /ask`), request/response schemas, validation rules, error responses, status codes, and a new shared `rag_service.py` module (resolves F9 — business logic no longer nested inside Inngest function bodies).
- Recorded two follow-on design decisions: D5 (`/ask` gets its own direct Gemini call rather than sharing `ctx.step.ai.infer` with the Inngest function — reasoned, not arbitrary duplication) and D6 (`/ingest` takes real file uploads per the earlier D3 direction, not a JSON path reference).
- Noted `/ask`'s design will finally resolve deferred finding F8 (empty-context short-circuit) since that code path is new anyway.
- No code changes this session — Step 5 is design-only, per the workflow. §8 is explicitly marked "not yet implemented."
- **Next:** Step 6 — implement the endpoints incrementally (branch `feature/fastapi-foundation` → `feature/ask-endpoint` → `feature/ingest-endpoint` → `feature/api-tests`, per §19's planned branching), starting with the `rag_service.py` extraction since both new endpoints and the existing Inngest functions depend on it.
- User asked to confirm D5 doesn't skip retrieval/grounding — clarified and confirmed as a misreading, no design change; recorded as §18 Q5.

### Session 7 — 2026-08-13
- User said start Step 6. Began with the `rag_service.py` extraction (base of the branch stack), since both `/ask`/`/ingest` and the existing Inngest functions depend on it.
- Branched `feature/rag-service-extraction` off `fix/rag-pipeline-hardening`.
- Added `PdfRef` model to `custom_types.py`; created `src/fastapiproject/rag_service.py` with `load_and_chunk_sources`, `embed_and_upsert`, `search_context`, `build_prompt`, `parse_cited_answer`, `SYSTEM_INSTRUCTION` — exactly matching the Step 5 design (§8.2).
- Rewrote `main.py`'s `_load`/`_upsert`/`_search` closures to call into `rag_service` instead of containing the logic inline; `rag_query_pdf_ai` now calls `rag_service.build_prompt`/`parse_cited_answer`. Removed now-unused `uuid`/`re` imports (their logic moved to `rag_service.py`).
- Noted and documented one subtlety while extracting: matching `.get("source_id", pdf_path)`'s exact fallback semantics with a Pydantic model isn't perfectly possible (can't distinguish "missing key" from "explicit null" post-parse) — used the closest faithful equivalent and wrote down the one never-hit edge case where it diverges, rather than silently claiming zero behavior change. Full detail under F9 in §12.
- **Fixed F9** (resolves the "business logic nested in Inngest functions" finding).
- Added `tests/test_rag_service.py` (9 cases, fully mocked).
- **Tests run:** `uv run pytest -q` → 20/20 passed (full project total).
- **Manual verification:** `main.py` imports cleanly; `rag_service.load_and_chunk_sources` re-run end-to-end against the real `test.pdf`, same output as pre-extraction.
- **Next:** implement `POST /ask` on a new `feature/ask-endpoint` branch stacked on top of this one.

### Session 8 — 2026-08-14
- Reverted a stray uncommitted one-line edit (`what`) accidentally left in `main.py`'s `rag_query_pdf_ai` from a prior session — unrelated to any planned work.
- Branched `feature/ask-endpoint` off `feature/rag-service-extraction`.
- Looked up exact API syntax via Context7 before writing any code: `google-genai`'s `client.models.generate_content(model, contents, config=types.GenerateContentConfig(system_instruction, temperature, max_output_tokens))` for sync, and confirmed `client.aio.models.embed_content`/`generate_content` for the later async pass; `qdrant-client`'s `AsyncQdrantClient` (mirrors the sync client's method names, all awaited).
- **Implemented `POST /ask`** via TDD (red-green per unit): added `rag_service.generate_answer` (direct Gemini call, Decision D5) and `rag_service.answer_question` (orchestrates `search_context` → empty-context short-circuit (resolves F8) → `build_prompt` → `generate_answer` → `parse_cited_answer`); added `AskRequest` to `custom_types.py`; created `src/fastapiproject/routers.py` (`ask_router`, per §8.5's routing-organization design) and wired it into `app` via `app.include_router(...)`. Maps `qdrant_client.http.exceptions.ApiException` to `503`; validation and other unhandled errors use FastAPI's automatic `422`/`500`.
- Confirmed via `main.app.openapi()['paths']` that `/ask` is registered correctly alongside `/api/inngest`.
- **Decision D7:** user asked to review Decision D4's deferred `AsyncQdrantClient` conversion now that `/ask` sends REST traffic through `search_context`/`embed_and_upsert` (D4's own trigger condition). Assistant found a refinement — `/ask`'s route handler is sync `def`, which FastAPI runs in a threadpool, not on the event loop, so D4's specific "blocks the event loop" concern doesn't reproduce the same way it did for Inngest steps — and presented a pros/cons table recommending deferral. User chose to convert now anyway. Full writeup, including the Inngest-closure interaction (`ctx.step.run`'s `maybe_await` behavior, confirmed by reading `transforms.py`), under D7 in §17.
- **Converted to async:** `vector_db.QdrantStorage` (→ `AsyncQdrantClient`, lazy `_ensure_collection()`), `data_loader.embed_texts` (→ `client.aio.models.embed_content`), `rag_service.embed_and_upsert`/`search_context`/`generate_answer`/`answer_question` (→ `async def`), `main.py`'s `_upsert`/`_search` Inngest closures (→ `async def`, no change needed to their `ctx.step.run(lambda: ...)` call sites), `routers.ask` (→ `async def`).
- Added `pytest-asyncio` as a dev dependency and `asyncio_mode = "auto"` to `pyproject.toml` — discovered and fixed a real gap first: a plain `async def test_...` function's body silently never ran without this (verified with a throwaway sanity test that raised `AssertionError` inside an async body and still reported "passed" until the plugin was added).
- **Tests run:** `uv run pytest -q` → 32/32 passed (12 new/updated in `test_rag_service.py`, `test_vector_db.py` rewritten for async (6 cases, including new coverage for the previously-untested `_ensure_collection`/`upsert`/`search` internals), `test_embed_texts.py` converted to async (3 cases), new `tests/test_ask_endpoint.py` (5 cases)). Also ran with `-W error::RuntimeWarning` to positively confirm no unawaited-coroutine warnings anywhere in the suite.
- **Could not verify live:** neither `localhost:6333` (Qdrant) nor a real `GEMINI_API_KEY` was available in this session — same class of gap Session 4 hit for the partial F5 fix. `main.py` confirmed to import cleanly and register `/ask` correctly; all behavior otherwise verified via mocks only. Flagged in §24, not silently skipped.
- **Next:** `feature/ingest-endpoint` (`POST /ingest` + `GET /ingest/{event_id}/status`, per §19's branch stack), or a live smoke test of `/ask`/the Inngest functions against a running Qdrant + real Gemini key first — user's call.

### Session 9 — 2026-08-16
- Branched `feature/ingest-endpoint` off `feature/ask-endpoint`.
- Looked up the exact Inngest REST API shape for the status endpoint via Context7 (`GET {api_origin}v1/events/{event_id}/runs` returns `{"data": [...]}`, each run has a `status` field with documented values `Queued`/`Running`/`Completed`/`Failed`/`Cancelled`); could not find an authoritative schema for the run object's output/error field names specifically (see D8's caveat) and no live Inngest Dev Server was reachable to check empirically, so `output`/`error` field access is a documented, flagged assumption rather than a confirmed fact.
- **Decision D8:** extracted `inngest.Inngest(...)` construction out of `main.py` into a new `src/fastapiproject/inngest_client.py` to avoid a circular import (`routers.py` now needs the client too, and `main.py` already imports `routers.py`). Also decided the `502`/`503` split for the status endpoint's error mapping. Full writeup in §17.
- **Implemented `POST /ingest`** via TDD: validates non-empty file list / `.pdf` extension + `application/pdf` content-type / ≤20MB size; sanitizes each filename to its basename and prefixes with `uuid4().hex` before saving under `PDF_UPLOAD_ROOT/uploads/`; re-validates the saved path through F1's `resolve_safe_pdf_path` (belt-and-suspenders, per §8.3); sends the existing `rag/ingest_pdf` event via `inngest_client.send(...)` — `rag_ingest_pdf` itself is untouched; returns `202` with `{event_id, status_url}`.
- **Implemented `GET /ingest/{event_id}/status`** via TDD: calls Inngest's REST API with `httpx`, maps no-runs-yet to `200`/`Queued` (not `404`, per §8.3's documented reasoning), `Completed`/`Failed` to `ingested`/`error`, connection failures to `503`, non-2xx upstream responses to `502` (D8).
- Added `IngestResponse`/`IngestStatusResponse` to `custom_types.py`; added `httpx` as an explicit dependency (was previously only transitive via `qdrant-client`/Starlette's `TestClient`); added `uploads/` to `.gitignore` (runtime data, same reasoning as `qdrant_storage/` in the Session 1 baseline commit); wired `ensure_upload_dir()` into the FastAPI `lifespan` alongside the existing F4 env-var check, per §8.3's documented design — verified with a throwaway `TestClient` run against a temp `PDF_UPLOAD_ROOT` that the `uploads/` directory actually gets created on startup.
- **Tests run:** `uv run pytest -q` → 42/42 passed (10 new in `tests/test_ingest_endpoint.py`, covering both endpoints end-to-end via `TestClient` with `inngest_client.send`/`httpx.AsyncClient.get` mocked and `PDF_UPLOAD_ROOT` monkeypatched to `tmp_path` for filesystem isolation). Confirmed via `main.app.openapi()['paths']` that all four routes (`/ask`, `/ingest`, `/ingest/{event_id}/status`, `/api/inngest`) register correctly.
- **Could not verify live:** neither a local Inngest Dev Server (`127.0.0.1:8288`) nor Qdrant (`localhost:6333`) was reachable this session — same class of gap as Sessions 4 and 8. The status endpoint's run-object field assumption (D8) specifically needs a live check before this is demo-ready; flagged in §24, not silently assumed correct.
- **Next:** `feature/api-tests` (broader endpoint test coverage, resolves F6) is the last item in §19's branch stack — or a live smoke test of all three endpoints against a running Inngest Dev Server + Qdrant + real Gemini key first, to close out the verification gaps from Sessions 4/8/9 in one pass. User's call.

### Session 11 — 2026-08-17
- Branched `feature/api-tests` off `feature/ingest-endpoint` — the last planned branch in §19's stack, closing out finding F6.
- Reviewed existing coverage for all three endpoints and identified genuine gaps rather than re-testing what already existed: `/ask`'s `score_threshold` bounds and its unhandled-error → `500` path; `/ingest`'s multi-file-in-one-request case, a missing-filename rejection, and an `inngest_client.send` failure → `500` path; and a cross-endpoint concern no single-endpoint test covered — that the pieces actually compose together end-to-end, not just that the router calls the right mocked function.
- Added the edge-case tests to `tests/test_ask_endpoint.py` (2 new) and `tests/test_ingest_endpoint.py` (3 new) — all passed immediately against the existing implementation, confirming correctness rather than fixing a bug.
- Added `tests/test_full_flow.py` (2 new): exercises `/ask`'s real call chain (`routers.ask` → `rag_service.answer_question` → `search_context`/`build_prompt`/`generate_answer`/`parse_cited_answer`) with only the genuine external boundaries mocked (`vector_db.AsyncQdrantClient`, `data_loader.client.aio.models`) — unlike every other `/ask` test, which stubs `rag_service.answer_question` itself and so never proves the internal pieces wire together correctly. Covers both a normal grounded answer and F8's empty-context short-circuit.
- Noted but did not attempt: testing `rag_ingest_pdf`/`rag_query_pdf_ai` (the Inngest function bodies themselves, as opposed to the `rag_service` functions they call) end-to-end would need Inngest's own test harness or a live Dev Server — out of scope for this project's unit-test suite, consistent with how Inngest-orchestrated code has been verified live rather than unit-tested throughout this project.
- **Tests run:** `uv run pytest -q` → 49/49 passed (up from 42). Also ran with `-W error::RuntimeWarning` to confirm no unawaited-coroutine warnings.
- **Next:** the branch stack planned in §19 is complete. Natural next steps: Step 7 (create the GitHub remote/repo, per Decision D2) now that Steps 1–6 are done, or a deliberate root-cause investigation of the transient `/ask` `500`/hang observed in Session 10 if it recurs (see §24). User's call.

### Session 12 — 2026-08-17
- Built the project's first UI: a Streamlit frontend, on branch `feature/streamlit-frontend`, built in an isolated git worktree (`.worktrees/feature-streamlit-frontend`) via subagent-driven-development, stacked on `feature/api-tests`.
- New files: `src/fastapiproject/api_client.py` (three thin synchronous `httpx` wrappers around the REST endpoints — `ask`, `ingest`, `check_status` — sharing a common `_request`/`ApiError` foundation) and `src/fastapiproject/streamlit_app.py` (two tabs: "Ask a Question", "Upload PDFs").
- Built a deliberate visual design system rather than default Streamlit styling: sage-gray/white/teal-pine/brick-red palette, Source Serif 4 + IBM Plex Sans/Mono typefaces, a responsive max-width layout, and a citation-chip component as the signature visual element for cited sources. Full rationale lives in `docs/superpowers/specs/2026-08-17-streamlit-frontend-design.md` §4.3.
- **Bugs found and fixed:** three real bugs, via live verification against the actual running backend and (for the third) a real browser pass, both caught by the subagent-driven-development review loop — task-scoped reviewers using live/real verification, not just diff-reading — before merge:
  1. `api_client._request()` never passed an explicit `timeout` to `httpx.request`, so httpx's ~5-second default applied. The real backend's `/ask` endpoint (Gemini generation) takes 13-90 seconds to respond, so every real Ask-tab click would have failed with "Could not reach the backend" even though the backend was healthy. Fixed by defaulting `_request` to a 120-second timeout via `kwargs.setdefault("timeout", 120)`.
  2. The Upload tab's "Check again" button (shown when ingestion is still running after the initial ~30s poll budget) called `st.rerun()`, but the surrounding poll/render logic was gated behind the Upload button's own per-run return value — a plain local variable, not persisted state. A real Streamlit rerun-semantics bug: on the "Check again" rerun, the Upload button naturally evaluates `False` (a button is only `True` on the run its own click triggered), so the whole block was skipped and the in-progress event id/status were silently lost — the button reset the tab instead of re-checking it. Fixed by moving `ingest_event_id`/`ingest_status` into `st.session_state`, so they survive reruns triggered by other widgets. (A follow-up gap in this same fix — the poll block re-ran on *any* rerun anywhere in the app, not just Upload-tab interactions, because it was gated on `if event_id:` alone — was caught in a later review pass and closed with an explicit `ingest_poll_requested` session-state flag, consumed once per request.)
  3. Streamlit auto-applies its own dark theme when the viewer's OS/browser prefers dark mode, fighting the app's injected CSS: headings/labels rendered near-illegible (confirmed via `getComputedStyle` — `h1` color came back `rgb(250,250,250)` instead of the intended `#1B2420`, font-family came back as Streamlit's default instead of Source Serif 4). Found during the controller's browser pass. Fixed via `.streamlit/config.toml`'s native `[theme]` section (`base="light"` plus the exact color/font tokens), which Streamlit's own docs confirm "will be applied by default, overriding the included Light and Dark themes."
- All three bugs are worth noting as the review process working as intended, not just "bugs happened" — live/real verification and a real browser pass caught defects a diff-read or mocked test would have missed.
- Run commands, two separate processes: `uv run uvicorn fastapiproject.main:app --reload` (backend, port 8000) and `uv run streamlit run src/fastapiproject/streamlit_app.py` (frontend, port 8501 by default, **must be launched from the project root**, where `.streamlit/config.toml` lives — running it from a different working directory means the theme config isn't found and bug 3's illegibility returns). `BACKEND_URL` env var (default `http://127.0.0.1:8000`) is how the frontend finds the backend — also what makes separate deployment possible later without a code change.
- **Manual verification (Tasks 4-6 + controller browser pass):** the page shell served successfully (Task 4's boot check: HTTP 200, page-shell HTML served, no startup traceback). The Ask tab's `api_client.ask()` round-trip succeeded live against the real backend, returning a real grounded answer with populated `sources` (Task 5). A real PDF (`test.pdf`) was ingested end-to-end via `api_client.ingest()` + `check_status()` polling, reaching `Completed` with a real chunk count (Task 6). Backend-down and invalid-upload error paths were confirmed to surface `st.error(...)` with the backend's actual message rather than a raw traceback, via the `ApiError` handling exercised in Tasks 5/6, per their reports. **Browser-rendered visuals confirmed** (after bug 3's fix, via the controller's own browser pass — not by any implementer subagent, none of which had browser/visual capability): legible headings/labels/tabs in the correct fonts and ink color, correctly-teal sliders and buttons, and a real live `/ask` round-trip rendering a citation chip (white card, brick-red left border, monospace filename) exactly as designed.
- **Testing:** no automated tests for `streamlit_app.py` — a deliberate, spec-authorized scope decision (UI rendering glue over Streamlit primitives, verified live/manually instead of via TDD, consistent with how this project has handled other manually-verified-only paths — see §15's Manual verification notes). `api_client.py` has full TDD coverage (`tests/test_api_client.py`, 6 tests).
- **Tests run:** `uv run pytest -q` → 55/55 passed (up from 49; +6 for `api_client.py`).
- The "Streamlit dependency declared but unused" line in Known Limitations (§16) is no longer true — updated to reflect the new frontend.
- **Next:** Step 7 — create the GitHub remote (Decision D2 said to wait until Steps 1-6 were done; they now are, and the frontend exists too).

## 23. Before/After Architecture

*(populated as changes land — none yet)*

## 24. Remaining Technical Debt

Tracks 1:1 with open items in §12 until each is fixed and moved to §13.

**D4's trigger condition fired and was resolved in Session 8** — see Decision D7. The `AsyncQdrantClient`/async-Gemini conversion is done, but **not yet verified against a live Qdrant instance or a real Gemini API key** (neither was reachable/configured that session, and still isn't as of Session 9). Recommend a live smoke test of `/ask` (and the still-sync-but-now-calling-async-code Inngest functions) before relying on this in a demo — this is the same class of gap Session 4 left open for F5's partial fix, now inherited by the full fix.

**Session 9 addition:** `GET /ingest/{event_id}/status`'s parsing of the run object's `output`/`error` fields (see D8) is based on Inngest's documentation and example code, not a live response — no local Inngest Dev Server was reachable this session either. Before relying on this endpoint in a demo, run `POST /ingest` against a real Inngest Dev Server + Qdrant, then poll `GET /ingest/{event_id}/status` and confirm the actual run JSON matches what `routers.ingest_status` expects (`status`, `output.ingested`, `error`) — adjust the field lookups if the real shape differs.

**✅ Resolved (Session 10, 2026-08-17):** ran the full stack live for the first time — Qdrant, a real Inngest Dev Server, and a real `GEMINI_API_KEY`, all in a separate terminal outside this assistant's session. `POST /ingest` → `GET /ingest/{event_id}/status` → `POST /ask` all confirmed working end-to-end against a real PDF. D8's `run.get("output").get("ingested")` field assumption was correct as written (`{"status":"Completed","ingested":4,"error":null}` came back exactly as designed) — no code change needed. One `/ask` call with `top_k=8` returned a `500` and a `top_k=7` call hung until a 2-minute client timeout; retries at `top_k=5,6` succeeded immediately, and the user confirmed on retry "it works as expected now" — most likely transient Gemini API latency/flakiness rather than a reproducible bug, but not root-caused (no traceback captured — the running `uvicorn` process's terminal was outside this session's visibility, and no `GEMINI_API_KEY` was available in this session's shell to reproduce it directly). Worth a second look if it recurs.

## 25. Recommended Next Steps

1. Git baseline commit of current working code (resolves F10).
2. Branch `fix/rag-pipeline-hardening`, fix F1 (path validation) and F2 (model id) first — highest severity.
3. Add tests alongside each fix (resolves F6 incrementally).
4. Fix F3/F4/F5.
5. Proceed to Step 4 (explain current FastAPI usage) → Step 5 (design REST API).
