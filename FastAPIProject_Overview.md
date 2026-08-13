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

## 8. API Documentation

*(To be completed in Step 6, after endpoints are designed in Step 5 and implemented.)*

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
| F5 | Medium | Performance | ⚠️ Partially fixed (Session 4) — singleton client done; async-client conversion deferred, see D4 |
| F6 | Medium | Testing | Zero automated tests in the project |
| F7 | Low | Correctness | Query function's `fn_id="RAG: Query pdf"` and its trigger event `rag/ingest_pdf_ai` are misleading/inconsistent names (ingest vs query) |
| F8 | Low | UX/Correctness | Empty search results still get sent into the LLM prompt as an empty context block rather than short-circuiting with a "no relevant context" response |
| F9 | Low | Maintainability | Business logic (`_load`, `_upsert`, `_search`) is nested inside Inngest function bodies in `main.py` rather than a separate service/module layer |
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

### F7, F8, F9 — deferred, low severity
Documented for awareness; will be addressed opportunistically alongside related work rather than as standalone fixes, to avoid unnecessary churn on working code (per project constraints).

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

## 14. Features Added

*(none yet)*

## 15. Tests

**Test infra:** `pytest` added as a dev dependency via `uv add --dev pytest` (Session 2). Run with `uv run pytest`.

| File | Covers | Cases | Result |
|---|---|---|---|
| `tests/test_data_loader.py` | `resolve_safe_pdf_path` (F1 fix) | relative path inside root resolves; `..` traversal outside root rejected (`ValueError`); absolute path outside root rejected (`ValueError`); missing file inside root raises `FileNotFoundError` | 4/4 passed |
| `tests/test_embed_texts.py` | `embed_texts` (F3 fix), mocked `client.models.embed_content` | correct-length response returns matching vectors; short response raises `RuntimeError`; `None`-valued embedding raises `RuntimeError` | 3/3 passed |
| `tests/test_main_startup.py` | `lifespan` startup validation (F4 fix), via `TestClient` + `monkeypatch` | app starts with `GEMINI_API_KEY` set; app raises `RuntimeError` at startup when it's unset | 2/2 passed |
| `tests/test_vector_db.py` | `get_storage()` singleton (F5 fix), mocked `QdrantStorage` class | constructs only once across repeated calls; reuses an existing singleton without re-constructing | 2/2 passed |

**Running total:** `uv run pytest -q` → 11/11 passed (Session 4).

**Manual verification (Session 2):** `load_and_chunk_pdf('test.pdf')` re-run against the real sample file with the real Gemini API key loaded — succeeded (1 chunk extracted), confirming F1's fix didn't regress the already-working ingestion path. `load_and_chunk_pdf('../../test.pdf')` confirmed rejected.

**Manual verification (Session 3):** `embed_texts(['hello world', 'second chunk of text'])` re-run against the real Gemini API — returned 2 vectors, dim 3072 each, confirming F3's fix doesn't regress the successful embedding path.

**Manual verification (Session 4):** Confirmed `main.py` still imports cleanly after swapping `QdrantStorage()` calls for `get_storage()`. Could **not** verify end-to-end against a real Qdrant instance — `localhost:6333` was unreachable this session (Qdrant not running). Recommend re-running `_upsert`/`_search` manually once Qdrant is up, before relying on this in a demo.

## 16. Known Limitations (current, as-is)

- No REST API — only reachable via Inngest events (see §7.3 for exactly how an event reaches this app).
- No auth/rate limiting.
- No upload size limits.
- Streamlit dependency declared but unused.
- README.md is empty.
- `uv run fastapiproject` console script is broken (F11) — use `uv run uvicorn fastapiproject.main:app --reload` instead.
- No incoming validation on Inngest event payloads (raw dict access; see §7.5) — will be addressed structurally by Step 5's Pydantic request models.
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

## 18. Questions & Decisions

Q&A log, chronological:

1. **Q:** Doc source format (see D1). **A:** Markdown.
2. **Q:** GitHub repo creation timing (see D2). **A:** Follow workflow order (hold off).
3. **Q:** F1 fix — which base directory should `pdf_path` be restricted to? **A (user):** "Let the option to upload be coming from the user via the streamlit UI, there we get the path which needs to be filled in" — i.e., long-term the path should come from an upload widget, not typed text. Interpreted as informing the *eventual* design (Step 5/6), while the *immediate* fix (this session) hardens the current Inngest code path with project-root containment — see D3.

## 19. Git Branching Strategy

*(To be defined at Step 3 kickoff — planned shape below, subject to change)*

```
main
 └── fix/rag-pipeline-hardening        (F1–F5 fixes, stacked or single — TBD by dependency)
      └── feature/fastapi-foundation   (Step 4/5 groundwork)
           └── feature/ask-endpoint    (Step 6: first REST endpoint)
                └── feature/api-tests  (Step 6: endpoint test coverage)
```

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

## 23. Before/After Architecture

*(populated as changes land — none yet)*

## 24. Remaining Technical Debt

Tracks 1:1 with open items in §12 until each is fixed and moved to §13.

**Trigger condition (from Decision D4):** if Step 6's REST API ends up calling `_upsert`/`_search` (or their successors) directly from a request handler that can receive concurrent traffic, revisit the deferred `AsyncQdrantClient` conversion immediately — don't let it sit as a someday-item once that's true.

## 25. Recommended Next Steps

1. Git baseline commit of current working code (resolves F10).
2. Branch `fix/rag-pipeline-hardening`, fix F1 (path validation) and F2 (model id) first — highest severity.
3. Add tests alongside each fix (resolves F6 incrementally).
4. Fix F3/F4/F5.
5. Proceed to Step 4 (explain current FastAPI usage) → Step 5 (design REST API).
