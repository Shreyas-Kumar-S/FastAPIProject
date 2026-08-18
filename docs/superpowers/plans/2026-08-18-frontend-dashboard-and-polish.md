# Frontend Dashboard & Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Settings popover, "Source: " citation labels, a Dashboard tab showing real Inngest ingestion timing plus Ask response timing, and hide Streamlit's Deploy button.

**Architecture:** One new backend endpoint (`GET /ingest/{event_id}/trace`) that looks up the Inngest run and queries the Dev Server's local GraphQL `runTrace` API for step-level timing, dedupes/relabels it for a normal user, and returns clean JSON. The frontend fetches this once (not per-rerun) right after an ingest completes and caches it in `st.session_state`, same pattern already used for `ingest_status`.

**Tech Stack:** Same as the existing frontend — `httpx` (now also used for a GraphQL POST, still just JSON over HTTP, no new library), Streamlit's native `st.popover`.

**Spec:** `docs/superpowers/specs/2026-08-18-frontend-dashboard-and-polish-design.md`

## Global Constraints

- The Inngest Dev Server's local GraphQL endpoint is `{api_origin}v0/gql` (confirmed live, not assumed) — `POST` with `{"query": ..., "variables": {"runID": ...}}`.
- `runTrace`'s `childrenSpans` contains duplicate entries per step name (one `duration: null` scheduling marker, one real-duration execution span) and internal bookkeeping spans prefixed `executor.` — both must be handled (spec §4.2).
- Step name → user-facing label mapping (spec §4.3): `"Load and Chunk pdf"` → `"Reading & splitting the document"`; `"Embedding and upsert"` → `"Generating embeddings & saving to the index"`; `"Finalization"` → `"Finishing up"`. Unmapped names fall back to their raw string, never dropped.
- Error handling for the new endpoint matches `ingest_status`'s existing convention exactly: `httpx.RequestError` → `503`; non-2xx response → `502`; no runs yet → `200` with empty/null data (not an error).
- `streamlit_app.py` changes get no automated tests (established, spec-authorized boundary — same as every other UI-only task on this project).
- The Dashboard tab's trace fetch happens **once**, right after the existing Upload-tab poll loop reaches `Completed` — never on every rerun (this is the exact bug class fixed in the prior branch's final review; don't reintroduce it).

---

### Task 1: Backend — `GET /ingest/{event_id}/trace`

**Files:**
- Modify: `src/fastapiproject/custom_types.py`
- Modify: `src/fastapiproject/routers.py`
- Test: `tests/test_ingest_endpoint.py`

**Interfaces:**
- Produces: `custom_types.TraceStep(label: str, duration_ms: int | None)`
- Produces: `custom_types.IngestTraceResponse(event_id: str, total_duration_ms: int | None, steps: list[TraceStep])`
- Produces: `GET /ingest/{event_id}/trace` — `response_model=IngestTraceResponse`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ingest_endpoint.py`, after the existing `test_status_maps_upstream_error_to_502` test:

```python
def test_trace_returns_empty_when_no_runs_yet(client):
    fake_response = httpx.Response(200, json={"data": []})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/trace")

    assert response.status_code == 200
    assert response.json() == {"event_id": "evt_1", "total_duration_ms": None, "steps": []}


def test_trace_returns_deduped_relabeled_steps(client):
    runs_response = httpx.Response(200, json={"data": [{"run_id": "run_1"}]})
    gql_response = httpx.Response(
        200,
        json={
            "data": {
                "runTrace": {
                    "duration": 1060,
                    "childrenSpans": [
                        {
                            "name": "Load and Chunk pdf",
                            "duration": None,
                            "startedAt": "2026-08-18T05:00:10.025Z",
                            "endedAt": "2026-08-18T05:00:10.035Z",
                        },
                        {
                            "name": "Load and Chunk pdf",
                            "duration": 13,
                            "startedAt": "2026-08-18T05:00:10.024Z",
                            "endedAt": "2026-08-18T05:00:10.037Z",
                        },
                        {
                            "name": "Embedding and upsert",
                            "duration": 788,
                            "startedAt": "2026-08-18T05:00:10.173Z",
                            "endedAt": "2026-08-18T05:00:10.961Z",
                        },
                        {
                            "name": "Finalization",
                            "duration": None,
                            "startedAt": "2026-08-18T05:00:11.077Z",
                            "endedAt": "2026-08-18T05:00:11.082Z",
                        },
                        {
                            "name": "executor.nonstep",
                            "duration": 10,
                            "startedAt": "2026-08-18T05:00:10.962Z",
                            "endedAt": "2026-08-18T05:00:11.084Z",
                        },
                    ],
                }
            }
        },
    )
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=runs_response)), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=gql_response)):
        response = client.get("/ingest/evt_1/trace")

    assert response.status_code == 200
    body = response.json()
    assert body["total_duration_ms"] == 1060
    assert body["steps"] == [
        {"label": "Reading & splitting the document", "duration_ms": 13},
        {"label": "Generating embeddings & saving to the index", "duration_ms": 788},
        {"label": "Finishing up", "duration_ms": 5},
    ]


def test_trace_falls_back_to_raw_name_for_unmapped_step(client):
    runs_response = httpx.Response(200, json={"data": [{"run_id": "run_1"}]})
    gql_response = httpx.Response(
        200,
        json={
            "data": {
                "runTrace": {
                    "duration": 42,
                    "childrenSpans": [
                        {
                            "name": "Some Future Step",
                            "duration": 7,
                            "startedAt": "2026-08-18T05:00:10.000Z",
                            "endedAt": "2026-08-18T05:00:10.007Z",
                        },
                    ],
                }
            }
        },
    )
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=runs_response)), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=gql_response)):
        response = client.get("/ingest/evt_1/trace")

    assert response.json()["steps"] == [{"label": "Some Future Step", "duration_ms": 7}]


def test_trace_maps_connection_failure_to_503(client):
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("nope"))):
        response = client.get("/ingest/evt_1/trace")

    assert response.status_code == 503


def test_trace_maps_upstream_error_to_502(client):
    fake_response = httpx.Response(500, json={})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/trace")

    assert response.status_code == 502
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_endpoint.py -k trace -v`
Expected: all 5 new tests FAIL with `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

Add to `src/fastapiproject/custom_types.py`, at the end of the file:

```python
class TraceStep(pydantic.BaseModel):
    label: str
    duration_ms: int | None = None

class IngestTraceResponse(pydantic.BaseModel):
    event_id: str
    total_duration_ms: int | None = None
    steps: list[TraceStep] = []
```

In `src/fastapiproject/routers.py`:

1. Update the import line:
```python
from fastapiproject.custom_types import (
    AskRequest,
    IngestResponse,
    IngestStatusResponse,
    IngestTraceResponse,
    QueryResult,
    TraceStep,
)
```

2. Add `from datetime import datetime` to the top-level imports.

3. Add these module-level constants near `MAX_PDF_SIZE_BYTES`:

```python
STEP_LABELS = {
    "Load and Chunk pdf": "Reading & splitting the document",
    "Embedding and upsert": "Generating embeddings & saving to the index",
    "Finalization": "Finishing up",
}


def _span_duration_ms(span: dict) -> int | None:
    duration = span.get("duration")
    if duration is not None:
        return duration
    started = span.get("startedAt")
    ended = span.get("endedAt")
    if started and ended:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return int((end_dt - start_dt).total_seconds() * 1000)
    return None
```

4. Add the endpoint at the end of the file:

```python
@ingest_router.get("/ingest/{event_id}/trace", response_model=IngestTraceResponse)
async def ingest_trace(event_id: str) -> IngestTraceResponse:
    runs_url = f"{inngest_client.api_origin}v1/events/{event_id}/runs"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            runs_response = await client.get(runs_url)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Inngest API is unreachable") from exc

    if runs_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Inngest API returned an error")

    runs = runs_response.json().get("data", [])
    if not runs:
        return IngestTraceResponse(event_id=event_id, total_duration_ms=None, steps=[])

    run_id = runs[0].get("run_id")
    if not run_id:
        return IngestTraceResponse(event_id=event_id, total_duration_ms=None, steps=[])

    gql_url = f"{inngest_client.api_origin}v0/gql"
    query = """
        query($runID: String!) {
          runTrace(runID: $runID) {
            duration
            childrenSpans {
              name
              duration
              startedAt
              endedAt
            }
          }
        }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            trace_response = await client.post(gql_url, json={"query": query, "variables": {"runID": run_id}})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Inngest API is unreachable") from exc

    if trace_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Inngest API returned an error")

    trace_data = trace_response.json().get("data", {}).get("runTrace")
    if not trace_data:
        return IngestTraceResponse(event_id=event_id, total_duration_ms=None, steps=[])

    steps_by_name: dict[str, int | None] = {}
    order: list[str] = []
    for span in trace_data.get("childrenSpans", []):
        name = span.get("name", "")
        if not name or name.startswith("executor."):
            continue
        duration = _span_duration_ms(span)
        if name not in steps_by_name:
            order.append(name)
            steps_by_name[name] = duration
        elif steps_by_name[name] is None and duration is not None:
            steps_by_name[name] = duration

    steps = [TraceStep(label=STEP_LABELS.get(name, name), duration_ms=steps_by_name[name]) for name in order]

    return IngestTraceResponse(event_id=event_id, total_duration_ms=trace_data.get("duration"), steps=steps)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_endpoint.py -v`
Expected: all tests pass (14 existing + 5 new = 19)

- [ ] **Step 5: Run the full project suite**

Run: `uv run pytest -q`
Expected: 61 passed (56 existing + 5 new)

- [ ] **Step 6: Commit**

```bash
git add src/fastapiproject/custom_types.py src/fastapiproject/routers.py tests/test_ingest_endpoint.py
git commit -m "feat: add GET /ingest/{event_id}/trace endpoint"
```

---

### Task 2: `api_client.get_trace()`

**Files:**
- Modify: `src/fastapiproject/api_client.py`
- Test: `tests/test_api_client.py`

**Interfaces:**
- Consumes: `_request(method, path, **kwargs) -> httpx.Response`, `ApiError` (existing)
- Produces: `api_client.get_trace(event_id: str) -> IngestTraceResponse`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_client.py` (add `IngestTraceResponse` to the existing `custom_types` import):

```python
def test_get_trace_returns_parsed_trace():
    fake = _fake_response(
        200,
        {
            "event_id": "evt_1",
            "total_duration_ms": 1060,
            "steps": [{"label": "Reading & splitting the document", "duration_ms": 13}],
        },
    )

    with patch("httpx.request", return_value=fake) as mock_request:
        result = api_client.get_trace("evt_1")

    assert result == IngestTraceResponse(
        event_id="evt_1",
        total_duration_ms=1060,
        steps=[TraceStep(label="Reading & splitting the document", duration_ms=13)],
    )
    mock_request.assert_called_once_with("GET", f"{api_client.BACKEND_URL}/ingest/evt_1/trace")
```

(Add `TraceStep` to the same import line too.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py -k get_trace -v`
Expected: FAIL with `AttributeError: module 'fastapiproject.api_client' has no attribute 'get_trace'`

- [ ] **Step 3: Write the minimal implementation**

Update the import line in `src/fastapiproject/api_client.py`:

```python
from fastapiproject.custom_types import (
    IngestResponse,
    IngestStatusResponse,
    IngestTraceResponse,
    QueryResult,
)
```

Add after `check_status`:

```python
def get_trace(event_id: str) -> IngestTraceResponse:
    response = _request("GET", f"/ingest/{event_id}/trace")
    return IngestTraceResponse(**response.json())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_client.py -v`
Expected: 7 passed

- [ ] **Step 5: Run the full project suite**

Run: `uv run pytest -q`
Expected: 62 passed

- [ ] **Step 6: Commit**

```bash
git add src/fastapiproject/api_client.py tests/test_api_client.py
git commit -m "feat: add api_client.get_trace()"
```

---

### Task 3: Polish — hide Deploy button, "Source: " citation prefix

**Files:**
- Modify: `.streamlit/config.toml`
- Modify: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- None new — `render_citation_chips`'s output text changes; no signature change.

- [ ] **Step 1: Write the implementation**

Add to `.streamlit/config.toml` (append, don't touch the existing `[theme]` section):

```toml

[client]
toolbarMode = "viewer"
```

In `src/fastapiproject/streamlit_app.py`, change `render_citation_chips`:

```python
def render_citation_chips(sources: list[str]) -> None:
    if not sources:
        return
    chips = "".join(f'<span class="citation-chip">Source: {html.escape(s)}</span>' for s in sources)
    st.markdown(f'<div class="citation-chip-row">{chips}</div>', unsafe_allow_html=True)
```

(Only the chip's inner text changed — `{html.escape(s)}` → `Source: {html.escape(s)}` — everything else in the function is identical.)

- [ ] **Step 2: Verify**

Start the app (`uv run streamlit run src/fastapiproject/streamlit_app.py --server.headless true --server.port &lt;a-free-port&gt;` from the project root, boot-check log for no traceback, `curl` returns 200, then kill it — no browser access, so this is the same mechanical verification used for prior UI-only tasks).

- [ ] **Step 3: Commit**

```bash
git add .streamlit/config.toml src/fastapiproject/streamlit_app.py
git commit -m "feat: hide Streamlit Deploy button, prefix citation chips with Source:"
```

---

### Task 4: Settings popover in the Ask tab

**Files:**
- Modify: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- No change to `api_client.ask(...)`'s call — `top_k`/`score_threshold` still come from the same two `st.slider` calls, just relocated.

- [ ] **Step 1: Write the implementation**

Replace this block in `src/fastapiproject/streamlit_app.py`:

```python
with ask_tab:
    question = st.text_input("Question")

    col1, col2 = st.columns(2)
    with col1:
        top_k = st.slider("Top K", 1, 20, value=5)
    with col2:
        score_threshold = st.slider("Score threshold", 0.0, 1.0, value=0.5, step=0.05)

    if st.button("Ask", key="ask_button") and question:
```

with:

```python
with ask_tab:
    question = st.text_input("Question")

    with st.popover("⚙️ Settings"):
        top_k = st.slider("Top K", 1, 20, value=5)
        score_threshold = st.slider("Score threshold", 0.0, 1.0, value=0.5, step=0.05)

    if st.button("Ask", key="ask_button") and question:
```

(Everything after this line — the `try`/`except`/`else` block calling `api_client.ask(...)` — is unchanged.)

- [ ] **Step 2: Verify**

Same mechanical boot-check as Task 3 (fresh port, no traceback, `curl` 200, kill).

- [ ] **Step 3: Commit**

```bash
git add src/fastapiproject/streamlit_app.py
git commit -m "feat: move Top K/Score threshold into a Settings popover"
```

---

### Task 5: Ask-side response timing

**Files:**
- Modify: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- Produces: `st.session_state["last_ask_duration"]: float` (seconds) — Task 6 (Dashboard tab) reads this.

- [ ] **Step 1: Write the implementation**

Replace this block (the `if st.button("Ask", ...)` body) in `src/fastapiproject/streamlit_app.py`:

```python
    if st.button("Ask", key="ask_button") and question:
        try:
            result = api_client.ask(question, top_k=top_k, score_threshold=score_threshold)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.write(result.answers)
            render_citation_chips(result.sources)
```

with:

```python
    if st.button("Ask", key="ask_button") and question:
        start_time = time.perf_counter()
        try:
            result = api_client.ask(question, top_k=top_k, score_threshold=score_threshold)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.session_state["last_ask_duration"] = time.perf_counter() - start_time
            st.write(result.answers)
            render_citation_chips(result.sources)
```

(`time` is already imported at the top of the file from prior work — no new import needed.)

- [ ] **Step 2: Verify**

Same mechanical boot-check pattern. If a live backend is reachable, an additional real check is worthwhile: run a short ad hoc Python check confirming `time.perf_counter()` differences behave as expected is unnecessary (it's stdlib, not project code) — the boot check is sufficient for this task; Task 6's verification will exercise this value for real once the Dashboard tab can display it.

- [ ] **Step 3: Commit**

```bash
git add src/fastapiproject/streamlit_app.py
git commit -m "feat: capture Ask response time in session_state"
```

---

### Task 6: Dashboard tab + fetch-once trace wiring

**Files:**
- Modify: `src/fastapiproject/streamlit_app.py`

**Interfaces:**
- Consumes: `api_client.get_trace(event_id) -> IngestTraceResponse` (Task 2), `st.session_state["last_ask_duration"]` (Task 5)

- [ ] **Step 1: Write the implementation**

Change the tabs line:

```python
ask_tab, upload_tab = st.tabs(["Ask a Question", "Upload PDFs"])
```

to:

```python
ask_tab, upload_tab, dashboard_tab = st.tabs(["Ask a Question", "Upload PDFs", "Dashboard"])
```

In the `with upload_tab:` block, find:

```python
                    if status.status in ("Completed", "Failed"):
                        break
                    time.sleep(1.5)
            st.session_state["ingest_status"] = status
```

and replace with:

```python
                    if status.status in ("Completed", "Failed"):
                        break
                    time.sleep(1.5)
            st.session_state["ingest_status"] = status
            if status is not None and status.status == "Completed":
                try:
                    st.session_state["ingest_trace"] = api_client.get_trace(event_id)
                except api_client.ApiError:
                    st.session_state["ingest_trace"] = None
```

Append the new tab at the end of the file:

```python
def _format_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.2f} s"


with dashboard_tab:
    st.subheader("Document Processing")
    trace = st.session_state.get("ingest_trace")
    if trace is not None:
        st.metric("Total time", _format_duration(trace.total_duration_ms))
        for step in trace.steps:
            st.write(f"**{step.label}:** {_format_duration(step.duration_ms)}")
    else:
        st.write("Upload a PDF to see processing stats here.")

    st.subheader("Answering")
    last_ask_duration = st.session_state.get("last_ask_duration")
    if last_ask_duration is not None:
        st.metric("Last answer", f"{last_ask_duration:.2f} s")
    else:
        st.write("Ask a question to see timing here.")
```

- [ ] **Step 2: Manually verify in the browser**

With the FastAPI backend, Qdrant, and Inngest Dev Server running, and a real `GEMINI_API_KEY` available, run the app from the project root and use a real browser (this task's behavior — the fetch-once pattern, real Inngest data rendering — needs actual visual/interactive confirmation, not just a boot check):

1. Confirm three tabs render: "Ask a Question", "Upload PDFs", "Dashboard".
2. Upload a real PDF. After it completes, switch to the Dashboard tab — confirm "Document Processing" shows a total time and the three relabeled steps ("Reading & splitting the document", "Generating embeddings & saving to the index", "Finishing up") each with a duration.
3. Switch back to the Ask tab, interact with something (type in the question box, open Settings) — switch back to Dashboard and confirm the ingestion stats are still there and unchanged (proves the fetch-once caching works, not a live re-fetch on every rerun).
4. Open Settings (⚙️), confirm the two sliders are there and the main Ask tab view no longer shows them inline.
5. Ask a question, confirm the answer renders with citation chips reading "Source: filename.pdf" (not just the bare filename). Switch to Dashboard — confirm "Answering" now shows a real elapsed time.
6. Confirm the top-right "Deploy" button Streamlit normally shows is gone.

- [ ] **Step 3: Commit**

```bash
git add src/fastapiproject/streamlit_app.py
git commit -m "feat: add Dashboard tab with ingestion trace and Ask timing"
```

---

### Task 7: Document this work

**Files:**
- Modify: `FastAPIProject_Overview.md`

**Interfaces:**
- None (documentation only)

- [ ] **Step 1: Run the full test suite one more time**

Run: `uv run pytest -q`
Expected: 62 passed

- [ ] **Step 2: Update `FastAPIProject_Overview.md`**

Add a row to the Features Added table (§14) for this work: the Settings popover, "Source: " citation prefix, the new `GET /ingest/{event_id}/trace` endpoint (note its Inngest Dev Server GraphQL dependency and the explicit non-guarantee for Inngest Cloud), the Dashboard tab, Ask-side timing, and the hidden Deploy button. Add a Session changelog entry summarizing the work, referencing the "Ingested None chunks." bug fixed earlier the same session (a separate, already-committed fix — mention it happened, don't re-describe the whole investigation) and this dashboard/polish work as the session's second piece of work. Reference `docs/superpowers/specs/2026-08-18-frontend-dashboard-and-polish-design.md`.

- [ ] **Step 3: Commit**

```bash
git add FastAPIProject_Overview.md
git commit -m "docs: record the dashboard and frontend polish work"
```
