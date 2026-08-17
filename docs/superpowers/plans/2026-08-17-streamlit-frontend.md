# Streamlit Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the project's first UI — a Streamlit app with two tabs (ask a question, upload PDFs) that talks to the existing FastAPI backend over HTTP.

**Architecture:** A new thin `api_client.py` module wraps three HTTP calls (`ask`, `ingest`, `check_status`) to the running FastAPI backend, reusing the backend's own Pydantic response models. `streamlit_app.py` is UI-only and calls into `api_client` — it never calls `httpx` directly.

**Tech Stack:** Streamlit (already an unused declared dependency), `httpx` (already a dependency, used synchronously here — Streamlit scripts are not async), the project's existing `custom_types.py` models.

**Spec:** `docs/superpowers/specs/2026-08-17-streamlit-frontend-design.md`

## Global Constraints

- `BACKEND_URL` env var, default `http://127.0.0.1:8000` (spec §3).
- `api_client.py` is **synchronous** — plain `httpx.request(...)` calls, no `async`/`await`. Streamlit re-runs its script top-to-bottom per interaction; it is not an asyncio context (this is a plan-level decision, not explicit in the spec, made because the spec's examples never call `api_client` with `await`).
- Reuse `custom_types.QueryResult`, `IngestResponse`, `IngestStatusResponse` for parsing responses — do not redefine these shapes (spec §3).
- No CORS middleware changes to `main.py` — confirmed unnecessary (spec §7): `api_client`'s HTTP calls run server-side inside Streamlit's own Python process, not browser JS.
- No Dockerfile, CI/CD, or hosting changes in this plan (spec §7, §9).
- `streamlit_app.py` is not unit-tested (spec §8) — its tasks end in manual browser verification instead of a `pytest` run.

---

### Task 1: `api_client.ask()` — plus the shared `ApiError`/`_request` foundation

**Files:**
- Create: `src/fastapiproject/api_client.py`
- Test: `tests/test_api_client.py`

**Interfaces:**
- Produces: `api_client.BACKEND_URL: str` (module constant, `os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")`)
- Produces: `api_client.ApiError(Exception)` with attributes `status_code: int | None` and `detail: str`
- Produces: `api_client._request(method: str, path: str, **kwargs) -> httpx.Response` — raises `ApiError` on non-2xx or connection failure; kwargs are passed straight through to `httpx.request`
- Produces: `api_client.ask(question: str, top_k: int = 5, score_threshold: float = 0.5) -> QueryResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_client.py`:

```python
from unittest.mock import patch

import httpx
import pytest

from fastapiproject import api_client
from fastapiproject.custom_types import QueryResult


def _fake_response(status_code, json_body):
    return httpx.Response(status_code, json=json_body)


def test_ask_returns_parsed_query_result():
    fake = _fake_response(200, {"answers": "the answer", "sources": ["doc1"], "num_contexts": 1})

    with patch("httpx.request", return_value=fake) as mock_request:
        result = api_client.ask("what is x?", top_k=3, score_threshold=0.4)

    assert result == QueryResult(answers="the answer", sources=["doc1"], num_contexts=1)
    mock_request.assert_called_once_with(
        "POST",
        f"{api_client.BACKEND_URL}/ask",
        json={"question": "what is x?", "top_k": 3, "score_threshold": 0.4},
    )


def test_ask_raises_api_error_with_backend_detail_on_4xx():
    fake = _fake_response(422, {"detail": "question too short"})

    with patch("httpx.request", return_value=fake):
        with pytest.raises(api_client.ApiError) as exc_info:
            api_client.ask("")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "question too short"


def test_ask_raises_api_error_with_none_status_on_connection_failure():
    with patch("httpx.request", side_effect=httpx.ConnectError("nope")):
        with pytest.raises(api_client.ApiError) as exc_info:
            api_client.ask("q")

    assert exc_info.value.status_code is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_client.py -v`
Expected: `ModuleNotFoundError: No module named 'fastapiproject.api_client'` (or `ImportError`) for all three tests.

- [ ] **Step 3: Write the minimal implementation**

Create `src/fastapiproject/api_client.py`:

```python
import os

import httpx

from fastapiproject.custom_types import QueryResult

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")


class ApiError(Exception):
    def __init__(self, status_code: int | None, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"{BACKEND_URL}{path}"
    try:
        response = httpx.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise ApiError(None, f"Could not reach the backend at {BACKEND_URL}") from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(response.status_code, detail)

    return response


def ask(question: str, top_k: int = 5, score_threshold: float = 0.5) -> QueryResult:
    response = _request(
        "POST",
        "/ask",
        json={"question": question, "top_k": top_k, "score_threshold": score_threshold},
    )
    return QueryResult(**response.json())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_client.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/fastapiproject/api_client.py tests/test_api_client.py
git commit -m "feat: add api_client.ask() with shared ApiError/_request foundation"
```

---

### Task 2: `api_client.ingest()`

**Files:**
- Modify: `src/fastapiproject/api_client.py`
- Test: `tests/test_api_client.py`

**Interfaces:**
- Consumes: `_request(method, path, **kwargs) -> httpx.Response` and `ApiError` from Task 1
- Produces: `api_client.ingest(files: list[tuple[str, bytes]]) -> IngestResponse`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_client.py` (add `IngestResponse` to the existing import from `fastapiproject.custom_types`):

```python
def test_ingest_sends_multipart_files_and_returns_parsed_response():
    fake = _fake_response(202, {"event_id": "evt_1", "status_url": "/ingest/evt_1/status"})

    with patch("httpx.request", return_value=fake) as mock_request:
        result = api_client.ingest([("doc.pdf", b"%PDF-1.4 hello")])

    assert result == IngestResponse(event_id="evt_1", status_url="/ingest/evt_1/status")
    mock_request.assert_called_once_with(
        "POST",
        f"{api_client.BACKEND_URL}/ingest",
        files=[("files", ("doc.pdf", b"%PDF-1.4 hello", "application/pdf"))],
    )


def test_ingest_sends_multiple_files_as_repeated_form_field():
    fake = _fake_response(202, {"event_id": "evt_2", "status_url": "/ingest/evt_2/status"})

    with patch("httpx.request", return_value=fake) as mock_request:
        api_client.ingest([("a.pdf", b"a-bytes"), ("b.pdf", b"b-bytes")])

    sent_files = mock_request.call_args.kwargs["files"]
    assert sent_files == [
        ("files", ("a.pdf", b"a-bytes", "application/pdf")),
        ("files", ("b.pdf", b"b-bytes", "application/pdf")),
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_client.py -v -k ingest`
Expected: FAIL with `AttributeError: module 'fastapiproject.api_client' has no attribute 'ingest'`

- [ ] **Step 3: Write the minimal implementation**

Add to `src/fastapiproject/api_client.py` (update the import line to include `IngestResponse`, and add the function after `ask`):

```python
from fastapiproject.custom_types import IngestResponse, QueryResult
```

```python
def ingest(files: list[tuple[str, bytes]]) -> IngestResponse:
    multipart_files = [("files", (name, content, "application/pdf")) for name, content in files]
    response = _request("POST", "/ingest", files=multipart_files)
    return IngestResponse(**response.json())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_client.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/fastapiproject/api_client.py tests/test_api_client.py
git commit -m "feat: add api_client.ingest()"
```

---

### Task 3: `api_client.check_status()`

**Files:**
- Modify: `src/fastapiproject/api_client.py`
- Test: `tests/test_api_client.py`

**Interfaces:**
- Consumes: `_request(method, path, **kwargs) -> httpx.Response` from Task 1
- Produces: `api_client.check_status(event_id: str) -> IngestStatusResponse`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_client.py` (add `IngestStatusResponse` to the existing import):

```python
def test_check_status_returns_parsed_status():
    fake = _fake_response(
        200, {"event_id": "evt_1", "status": "Completed", "ingested": 5, "error": None}
    )

    with patch("httpx.request", return_value=fake) as mock_request:
        result = api_client.check_status("evt_1")

    assert result == IngestStatusResponse(event_id="evt_1", status="Completed", ingested=5, error=None)
    mock_request.assert_called_once_with("GET", f"{api_client.BACKEND_URL}/ingest/evt_1/status")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py -v -k check_status`
Expected: FAIL with `AttributeError: module 'fastapiproject.api_client' has no attribute 'check_status'`

- [ ] **Step 3: Write the minimal implementation**

Update the import line in `src/fastapiproject/api_client.py`:

```python
from fastapiproject.custom_types import IngestResponse, IngestStatusResponse, QueryResult
```

Add after `ingest`:

```python
def check_status(event_id: str) -> IngestStatusResponse:
    response = _request("GET", f"/ingest/{event_id}/status")
    return IngestStatusResponse(**response.json())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_client.py -v`
Expected: 6 passed

- [ ] **Step 5: Run the full project suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: 55 passed (49 existing + 6 new)

- [ ] **Step 6: Commit**

```bash
git add src/fastapiproject/api_client.py tests/test_api_client.py
git commit -m "feat: add api_client.check_status()"
```

---

### Task 4: `streamlit_app.py` — Ask tab

**Files:**
- Create: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- Consumes: `api_client.ask(question, top_k, score_threshold) -> QueryResult`, `api_client.ApiError`

- [ ] **Step 1: Write the implementation**

Create `src/fastapiproject/streamlit_app.py`:

```python
import streamlit as st

from fastapiproject import api_client

st.set_page_config(page_title="RAG PDF Assistant", page_icon="📄")
st.title("RAG PDF Assistant")

ask_tab, upload_tab = st.tabs(["Ask a Question", "Upload PDFs"])

with ask_tab:
    question = st.text_input("Question")

    with st.expander("Advanced"):
        top_k = st.slider("Top K", 1, 20, value=5)
        score_threshold = st.slider("Score threshold", 0.0, 1.0, value=0.5, step=0.05)

    if st.button("Ask", key="ask_button") and question:
        try:
            result = api_client.ask(question, top_k=top_k, score_threshold=score_threshold)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.write(result.answers)
            if result.sources:
                with st.expander(f"Sources ({result.num_contexts} context chunks)"):
                    for source in result.sources:
                        st.write(f"- {source}")
```

- [ ] **Step 2: Manually verify in the browser**

With the FastAPI backend running (`uv run uvicorn fastapiproject.main:app --reload`) and a real `GEMINI_API_KEY`/Qdrant available, run:

```bash
uv run streamlit run src/fastapiproject/streamlit_app.py
```

In the opened browser tab:
1. Confirm the "Ask a Question" tab renders with a text input, an "Advanced" expander containing two sliders, and an "Ask" button.
2. Type a question and click "Ask" — confirm an answer renders, and (if sources exist) a "Sources" expander lists them.
3. Stop the `uvicorn` process, click "Ask" again — confirm `st.error` shows a "Could not reach the backend" message rather than a raw traceback. Restart `uvicorn` afterward.

- [ ] **Step 3: Commit**

```bash
git add src/fastapiproject/streamlit_app.py
git commit -m "feat: add Streamlit Ask tab"
```

---

### Task 5: `streamlit_app.py` — Upload tab with status polling

**Files:**
- Modify: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- Consumes: `api_client.ingest(files) -> IngestResponse`, `api_client.check_status(event_id) -> IngestStatusResponse`, `api_client.ApiError`

- [ ] **Step 1: Write the implementation**

Add to `src/fastapiproject/streamlit_app.py` (add `import time` at the top of the file, alongside the existing `import streamlit as st`):

```python
import time
```

Append inside the `with upload_tab:` block (add this after the `ask_tab` block, at the same indentation level as `with ask_tab:`):

```python
with upload_tab:
    uploaded_files = st.file_uploader("PDFs", accept_multiple_files=True, type=["pdf"])

    if st.button("Upload", key="upload_button") and uploaded_files:
        files = [(f.name, f.getvalue()) for f in uploaded_files]
        try:
            ingest_result = api_client.ingest(files)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.write(f"Event ID: `{ingest_result.event_id}`")

            with st.spinner("Ingesting..."):
                status = None
                for _ in range(20):
                    try:
                        status = api_client.check_status(ingest_result.event_id)
                    except api_client.ApiError as exc:
                        st.error(exc.detail)
                        status = None
                        break
                    if status.status in ("Completed", "Failed"):
                        break
                    time.sleep(1.5)

            if status is not None:
                if status.status == "Completed":
                    st.success(f"Ingested {status.ingested} chunks.")
                elif status.status == "Failed":
                    st.error(status.error)
                else:
                    st.info(f"Still {status.status.lower()}...")
                    if st.button("Check again", key="check_again_button"):
                        st.rerun()
```

- [ ] **Step 2: Manually verify in the browser**

With `uvicorn` and `streamlit run` both still running (restart `streamlit run` to pick up the file change):

1. Confirm the "Upload PDFs" tab renders a file uploader (PDF-only) and an "Upload" button.
2. Upload a real PDF (e.g. `test.pdf` from the project root) and click "Upload" — confirm the event ID appears, a spinner runs, and it resolves to "Ingested N chunks."
3. Switch to the "Ask a Question" tab and ask something about the just-uploaded PDF — confirm the answer cites it (matches the manual verification already done in Session 10 via curl).
4. Upload a non-PDF file if the uploader allows it, or a file above 20MB, and confirm `st.error` shows the backend's validation message rather than a raw traceback.

- [ ] **Step 3: Commit**

```bash
git add src/fastapiproject/streamlit_app.py
git commit -m "feat: add Streamlit Upload tab with status polling"
```

---

### Task 6: Document the frontend and finalize

**Files:**
- Modify: `FastAPIProject_Overview.md`

**Interfaces:**
- None (documentation only)

- [ ] **Step 1: Run the full test suite one more time**

Run: `uv run pytest -q`
Expected: 55 passed

- [ ] **Step 2: Update `FastAPIProject_Overview.md`**

Add a row to the Features Added table (§14) for the Streamlit frontend, referencing `api_client.py`/`streamlit_app.py`, the `BACKEND_URL` env var, and the two run commands (`uv run uvicorn fastapiproject.main:app --reload` + `uv run streamlit run src/fastapiproject/streamlit_app.py`). Add a Session changelog entry (§22) summarizing the work, the manual verification performed in Task 4/5's Step 2, and noting Step 7 (GitHub remote, Decision D2) as the next milestone now that the frontend exists too. Update the "Streamlit dependency declared but unused" line in Known Limitations (§16) — it is no longer unused.

- [ ] **Step 3: Commit**

```bash
git add FastAPIProject_Overview.md
git commit -m "docs: record the Streamlit frontend"
```
