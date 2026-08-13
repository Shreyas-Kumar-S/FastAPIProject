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

## 10. FastAPI Concepts Learned

*(populated from Step 4 onward)*

## 11. RAG Concepts Learned

- **Session 1:** Grounding/citation pattern — the prompt asks the model to both answer *and* self-report which numbered context chunks it used, which is then parsed back into human-readable sources. This is a cheap way to get attribution without a second retrieval-verification pass, but it trusts the model's self-report (it can hallucinate a `Used:` line that doesn't reflect what it actually relied on).
- **Session 1:** Chunk/embedding alignment invariant — `chunks`, `sources`, `ids`, `vecs`, `payloads` are all parallel lists indexed by position. Any step that changes list length without updating all four breaks retrieval silently (see finding F3).

## 12. Bugs / Issues Discovered (Session 1 review — prioritized)

| # | Severity | Area | What |
|---|---|---|---|
| F1 | Critical | Security | Unvalidated `pdf_path` from event data |
| F2 | Critical | Correctness | Unverified Gemini model id `gemini-3.6-flash` |
| F3 | High | Correctness | `embed_texts` result not checked for `None`/length mismatch before upsert |
| F4 | High | Reliability | `GEMINI_API_KEY` only validated at call time, not startup |
| F5 | Medium | Performance | `QdrantStorage()` re-created per call; sync client used inside async Inngest steps; `collection_exists` re-checked every construction |
| F6 | Medium | Testing | Zero automated tests in the project |
| F7 | Low | Correctness | Query function's `fn_id="RAG: Query pdf"` and its trigger event `rag/ingest_pdf_ai` are misleading/inconsistent names (ingest vs query) |
| F8 | Low | UX/Correctness | Empty search results still get sent into the LLM prompt as an empty context block rather than short-circuiting with a "no relevant context" response |
| F9 | Low | Maintainability | Business logic (`_load`, `_upsert`, `_search`) is nested inside Inngest function bodies in `main.py` rather than a separate service/module layer |
| F10 | Info | Git hygiene | Repo has no completed baseline commit (see §4) — must be fixed before branching |

### F1 — Unvalidated `pdf_path`
- **WHAT:** `pdf["pdf_path"]` from event payload is passed directly to `Path()` and read, with no restriction on location.
- **WHY:** Event payloads are external input (anything that can publish to the Inngest endpoint controls this path).
- **IMPACT:** Path traversal / arbitrary local file read (e.g. `../../etc/passwd`-style paths) once ingestion is reachable by less-trusted callers (which it will be once wrapped in a REST endpoint in Step 6).
- **SOLUTION:** Restrict ingestion to a fixed uploads directory; resolve and verify the final path stays inside it before reading.
- **VERIFICATION:** Unit test asserting a `..`-containing path is rejected; existing valid-path ingestion still works.

### F2 — Unverified model id
- **WHAT:** `model="gemini-3.6-flash"` in `main.py`.
- **WHY:** Model catalog names change; an invalid id fails at call time, not at startup or review time.
- **IMPACT:** Query pipeline breaks in production with an opaque API error.
- **SOLUTION:** Confirm the exact current model id against Gemini docs/API; make it a named constant (mirroring `EMBED_MODEL` in `data_loader.py`) so it's defined once.
- **VERIFICATION:** Manual query test against the real Gemini API returns a 200/valid completion.

### F3 — Silent embedding misalignment
- **WHAT:** `embed_texts` return type is `list[list[float|int] | None]`; `_upsert` and `_search` never check for `None` or length mismatches before zipping with `ids`/`payloads`/`sources`.
- **WHY:** All downstream code assumes parallel-list alignment (see §11).
- **IMPACT:** A single failed embedding shifts every subsequent id/payload pairing — wrong text gets attached to wrong vector, corrupting retrieval silently (no exception raised).
- **SOLUTION:** Validate embedding count/`None`-ness immediately after the Gemini call; raise a clear error rather than proceeding misaligned.
- **VERIFICATION:** Unit test with a mocked embedder returning a short/`None`-containing list asserts a raised error instead of silent corruption.

### F4 — API key validated too late
- **WHAT:** `os.environ["GEMINI_API_KEY"]` is read inside the query function body.
- **WHY:** Fails on first real request instead of at process startup.
- **IMPACT:** Confusing runtime `KeyError` deep in a request instead of a clear boot-time failure.
- **SOLUTION:** Validate required env vars once at startup (e.g. FastAPI startup hook), fail fast with a clear message.
- **VERIFICATION:** Start app with the var unset → clear startup error, not a buried `KeyError`.

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

*(none yet — Step 3 not started)*

## 14. Features Added

*(none yet)*

## 15. Tests

*(none yet — project currently has zero test files)*

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

## 18. Questions & Decisions

Q&A log, chronological:

1. **Q:** Doc source format (see D1). **A:** Markdown.
2. **Q:** GitHub repo creation timing (see D2). **A:** Follow workflow order (hold off).

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
- **Next:** Step 3 — create the git baseline commit, then fix findings in priority order starting with F1/F2, each on its own branch with tests.

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
