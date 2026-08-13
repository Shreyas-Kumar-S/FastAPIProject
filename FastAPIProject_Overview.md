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

## 7. FastAPI Architecture

*(To be completed in Step 4 — request lifecycle, ASGI, how `inngest.fast_api.serve` wires routes onto the `FastAPI()` app, ASGI vs WSGI, ASGI app object, ⇒ ties directly into the REST API design in Step 5.)*

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

## 11. RAG Concepts Learned

- **Session 1:** Grounding/citation pattern — the prompt asks the model to both answer *and* self-report which numbered context chunks it used, which is then parsed back into human-readable sources. This is a cheap way to get attribution without a second retrieval-verification pass, but it trusts the model's self-report (it can hallucinate a `Used:` line that doesn't reflect what it actually relied on).
- **Session 1:** Chunk/embedding alignment invariant — `chunks`, `sources`, `ids`, `vecs`, `payloads` are all parallel lists indexed by position. Any step that changes list length without updating all four breaks retrieval silently (see finding F3).

## 12. Bugs / Issues Discovered (Session 1 review — prioritized)

| # | Severity | Area | What |
|---|---|---|---|
| F1 | Critical | Security | ✅ Fixed (Session 2) — Unvalidated `pdf_path` from event data |
| F2 | Critical | Correctness | ✅ Verified working (Session 2, user tested against real API with their key) — Gemini model id `gemini-3.6-flash` |
| F3 | High | Correctness | ✅ Fixed (Session 3) — `embed_texts` result not checked for `None`/length mismatch before upsert |
| F4 | High | Reliability | ✅ Fixed (Session 3) — `GEMINI_API_KEY` only validated at call time, not startup |
| F5 | Medium | Performance | `QdrantStorage()` re-created per call; sync client used inside async Inngest steps; `collection_exists` re-checked every construction |
| F6 | Medium | Testing | Zero automated tests in the project |
| F7 | Low | Correctness | Query function's `fn_id="RAG: Query pdf"` and its trigger event `rag/ingest_pdf_ai` are misleading/inconsistent names (ingest vs query) |
| F8 | Low | UX/Correctness | Empty search results still get sent into the LLM prompt as an empty context block rather than short-circuiting with a "no relevant context" response |
| F9 | Low | Maintainability | Business logic (`_load`, `_upsert`, `_search`) is nested inside Inngest function bodies in `main.py` rather than a separate service/module layer |
| F10 | Info | Git hygiene | Repo has no completed baseline commit (see §4) — must be fixed before branching |

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
- **SOLUTION:** Share a single client instance (module-level or app-state), move the `collection_exists`/`create_collection` check to a one-time startup step, and evaluate `AsyncQdrantClient`.
- **VERIFICATION:** Existing search/upsert behavior unchanged; startup performs exactly one collection-existence check per process.

### F6 — No tests
- **WHAT / IMPACT / SOLUTION:** No automated tests exist anywhere. Every fix in Step 3 must land with a corresponding test so regressions are caught mechanically, not by re-reading code.
- **VERIFICATION:** `pytest` run showing new tests passing, added to this doc's Testing Notes section per change.

### F7, F8, F9 — deferred, low severity
Documented for awareness; will be addressed opportunistically alongside related work rather than as standalone fixes, to avoid unnecessary churn on working code (per project constraints).

### F10 — Git baseline
- **SOLUTION:** First commit on `main` will be "baseline: working RAG pipeline as-is" containing the currently-working code untouched, before any fix/feature branch is cut. This gives every subsequent PR a clean diff against real history.

## 13. Bugs Fixed

| # | Fixed in | Summary |
|---|---|---|
| F1 | Session 2, branch `fix/rag-pipeline-hardening` | Added `resolve_safe_pdf_path()` path-containment check before any PDF is read; see full writeup under F1 in §12. |
| F3 | Session 3, branch `fix/rag-pipeline-hardening` | `embed_texts` now validates embedding count and non-`None` values, raising `RuntimeError` instead of silently misaligning ids/vectors/payloads; see F3 in §12. |
| F4 | Session 3, branch `fix/rag-pipeline-hardening` | Added FastAPI `lifespan` startup check for `GEMINI_API_KEY`; app now fails fast at boot with a clear message instead of a buried `KeyError` mid-request; see F4 in §12. |

## 14. Features Added

*(none yet)*

## 15. Tests

**Test infra:** `pytest` added as a dev dependency via `uv add --dev pytest` (Session 2). Run with `uv run pytest`.

| File | Covers | Cases | Result |
|---|---|---|---|
| `tests/test_data_loader.py` | `resolve_safe_pdf_path` (F1 fix) | relative path inside root resolves; `..` traversal outside root rejected (`ValueError`); absolute path outside root rejected (`ValueError`); missing file inside root raises `FileNotFoundError` | 4/4 passed |
| `tests/test_embed_texts.py` | `embed_texts` (F3 fix), mocked `client.models.embed_content` | correct-length response returns matching vectors; short response raises `RuntimeError`; `None`-valued embedding raises `RuntimeError` | 3/3 passed |
| `tests/test_main_startup.py` | `lifespan` startup validation (F4 fix), via `TestClient` + `monkeypatch` | app starts with `GEMINI_API_KEY` set; app raises `RuntimeError` at startup when it's unset | 2/2 passed |

**Running total:** `uv run pytest -q` → 9/9 passed (Session 3).

**Manual verification (Session 2):** `load_and_chunk_pdf('test.pdf')` re-run against the real sample file with the real Gemini API key loaded — succeeded (1 chunk extracted), confirming F1's fix didn't regress the already-working ingestion path. `load_and_chunk_pdf('../../test.pdf')` confirmed rejected.

**Manual verification (Session 3):** `embed_texts(['hello world', 'second chunk of text'])` re-run against the real Gemini API — returned 2 vectors, dim 3072 each, confirming F3's fix doesn't regress the successful embedding path.

## 16. Known Limitations (current, as-is)

- No REST API — only reachable via Inngest events.
- No auth/rate limiting.
- No upload size limits.
- Streamlit dependency declared but unused.
- README.md is empty.
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

## 23. Before/After Architecture

*(populated as changes land — none yet)*

## 24. Remaining Technical Debt

Tracks 1:1 with open items in §12 until each is fixed and moved to §13.

## 25. Recommended Next Steps

1. Git baseline commit of current working code (resolves F10).
2. Branch `fix/rag-pipeline-hardening`, fix F1 (path validation) and F2 (model id) first — highest severity.
3. Add tests alongside each fix (resolves F6 incrementally).
4. Fix F3/F4/F5.
5. Proceed to Step 4 (explain current FastAPI usage) → Step 5 (design REST API).
