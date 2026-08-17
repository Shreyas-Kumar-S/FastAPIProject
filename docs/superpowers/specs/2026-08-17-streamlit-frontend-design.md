# Streamlit Frontend — Design Spec

**Date:** 2026-08-17
**Status:** Approved for implementation
**Branch (planned):** `feature/streamlit-frontend`, stacked on `feature/api-tests`

## 1. Purpose

Give the RAG backend (all three REST endpoints implemented as of Session 11) a usable UI, so PDFs can be uploaded and questions asked without curl. This is the first UI the project has had — everything so far has been REST/Inngest only.

This is also explicitly a **stepping stone toward deployment**: build the UI now → push both backend and frontend to GitHub (Step 7, Decision D2) → deploy later. Nothing in this spec attempts the deployment step itself (no Dockerfile, no hosting choice, no CI/CD) — see §7.

## 2. Framework and integration approach (decided)

- **Streamlit.** Matches the dependency already declared (and unused) in `pyproject.toml`, and the Streamlit-upload intent recorded in Session 2 (Decision D3).
- **HTTP calls to the running FastAPI backend**, not direct Python imports of `rag_service`. Streamlit runs as its own process (`streamlit run`); it calls `POST /ask`, `POST /ingest`, `GET /ingest/{event_id}/status` over HTTP, exactly as `curl` does today. Keeps the REST layer meaningful instead of bypassing it.

## 3. Architecture / file layout

```
src/fastapiproject/
├── api_client.py       # new - thin HTTP client, the testable layer
└── streamlit_app.py    # new - UI, two tabs
```

- `api_client.py` wraps the three backend calls using `httpx` (already a dependency). It reuses `custom_types.QueryResult`, `IngestResponse`, `IngestStatusResponse` directly to parse responses — same package, no schema duplication.
- `streamlit_app.py` is UI-only: renders widgets, calls into `api_client`, displays results/errors. No `httpx` calls live here directly.
- Base URL: `BACKEND_URL` env var, default `http://127.0.0.1:8000`, read the same way `PDF_UPLOAD_ROOT`/`GEMINI_API_KEY` already are (`os.environ.get(...)`, `.env` via `python-dotenv`).
- Run with `uv run streamlit run src/fastapiproject/streamlit_app.py`, as a second process alongside `uv run uvicorn fastapiproject.main:app --reload`. Documented in the overview doc next to the existing run command.

## 4. Components

### 4.1 `api_client.py`

Three functions, each a thin wrapper over one backend call:

- `ask(question: str, top_k: int = 5, score_threshold: float = 0.5) -> QueryResult`
  `POST {BACKEND_URL}/ask` with the JSON body.
- `ingest(files: list[tuple[str, bytes]]) -> IngestResponse`
  `POST {BACKEND_URL}/ingest` as multipart form data (`files` field, repeated for each tuple).
- `check_status(event_id: str) -> IngestStatusResponse`
  `GET {BACKEND_URL}/ingest/{event_id}/status`.

A small `ApiError(Exception)` class with two attributes — `status_code: int | None` and `detail: str` — is raised by all three on failure: `status_code` set to the response's HTTP status on a non-2xx response (`detail` taken from the backend's JSON `{"detail": ...}` body), or `status_code=None` with a fixed connection-failure message when the backend is unreachable (`httpx.RequestError`). All three functions route through one shared internal `_request(...)` helper so this mapping is written once, not three times (same "fail at one point of origin" principle already used elsewhere in this codebase, e.g. `embed_texts`).

### 4.2 `streamlit_app.py`

`st.set_page_config(...)`, two tabs via `st.tabs([...])`:

**Tab 1 — Ask a Question**
- `st.text_input` for the question.
- `st.expander("Advanced")` containing `st.slider("Top K", 1, 20, value=5)` (int) and `st.slider("Score threshold", 0.0, 1.0, value=0.5, step=0.05)` (float) — matches `AskRequest`'s validated bounds exactly, so the UI can't submit something the backend would reject.
- Submit button → `api_client.ask(...)`. On success: renders the answer text, then an expander listing cited `sources`. On `ApiError`: `st.error(f"...")` showing the backend's `detail`.

**Tab 2 — Upload PDFs**
- `st.file_uploader(accept_multiple_files=True, type=["pdf"])`.
- Submit button → `api_client.ingest(...)`. On success, shows `event_id` and starts polling.
- Polling: inside `st.spinner("Ingesting...")`, loop calling `api_client.check_status(event_id)` every ~1.5s, up to ~20 iterations (~30s total). Stops early on `status in ("Completed", "Failed")`. On `Completed`, shows the `ingested` chunk count; on `Failed`, shows the `error` via `st.error`. If still `Queued`/`Running` after the timeout, stops polling and shows a manual "Check again" button (`st.button`) that re-runs one status check on click — avoids blocking the Streamlit process indefinitely.
- On `ApiError` from the initial `ingest` call (422/413/500): `st.error(...)` with the backend's `detail`, no polling starts.

## 5. Data flow

```
Ask tab:     Streamlit widget -> api_client.ask() -> httpx.post(/ask) -> QueryResult -> render

Upload tab:  Streamlit widget -> api_client.ingest() -> httpx.post(/ingest) -> IngestResponse
                 -> loop: api_client.check_status() -> httpx.get(/ingest/{id}/status) -> IngestStatusResponse
                 -> render (Completed/Failed) or manual re-check button (still running)
```

## 6. Error handling

Every path either renders a result or a clear `st.error(...)` — no silent failures, no raw tracebacks shown to the user. Specifically:
- Backend unreachable (connection refused/timeout) → `st.error("Can't reach the backend at {BACKEND_URL} — is uvicorn running?")`.
- Any 4xx/5xx from the backend → `st.error(...)` using the response body's `detail` field (all three endpoints already return FastAPI's standard `{"detail": "..."}` shape on error, including the automatic `422`s).

## 7. Deployment-readiness notes (not implemented now, just accounted for)

- **`BACKEND_URL` being env-driven is what makes this deployable later** — Streamlit and FastAPI as two separately-hosted services just means setting `BACKEND_URL` to the real backend's public URL instead of `localhost`. No code change needed when that day comes.
- **CORS is not needed, now or after deployment.** `api_client.py`'s `httpx` calls run inside Streamlit's own Python server process, not browser JavaScript — the browser only ever talks to Streamlit itself. The Streamlit→FastAPI hop is server-to-server and isn't subject to the browser's same-origin policy. Recorded here explicitly so a future session doesn't add unnecessary CORS middleware to `main.py` on the mistaken assumption it's required.
- **Two separate long-running processes** (`uvicorn`, `streamlit run`) — "deploy" will eventually mean two deployments (or one host running both). Nothing in this design assumes they share a process or a filesystem beyond both reading the same `.env`/`BACKEND_URL` convention.
- **Explicitly out of scope for this pass:** Dockerfile, CI/CD, hosting platform choice, HTTPS/auth in front of either service. These are real future steps, deliberately deferred rather than half-built now — same pattern as Decision D2 deferring GitHub-remote creation until it was actually the next step.

## 8. Testing

- `api_client.py`'s three functions (`ask`, `ingest`, `check_status`) get real unit tests via TDD — mocked `httpx`, same pattern as every other test in this project (`tests/test_api_client.py`). This is the layer with actual logic (request building, response parsing, error mapping).
- `streamlit_app.py` itself is UI-rendering glue over Streamlit's own primitives (`st.text_input`, `st.spinner`, etc.) — not meaningfully unit-testable without heavy mocking of Streamlit internals that would test the mocks, not the app. Verified manually instead: run `streamlit run`, exercise both tabs against the real running backend in a browser, confirm the happy path and at least one error path (e.g. stop the backend, confirm the "can't reach backend" message) before calling this done. Same honest scope boundary already drawn for the Inngest function bodies (`rag_ingest_pdf`/`rag_query_pdf_ai`), which also aren't unit-tested for the same reason (framework orchestration, not business logic).

## 9. Out of scope (explicitly)

- Chat history / multi-turn conversation state.
- Authentication of any kind.
- Displaying ingested-document lists / delete/manage functionality.
- Anything from §7 (Docker, CI/CD, hosting).
