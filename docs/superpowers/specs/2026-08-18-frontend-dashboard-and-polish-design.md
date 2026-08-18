# Frontend Dashboard & Polish — Design Spec

**Date:** 2026-08-18
**Status:** Approved for implementation (design confirmed conversationally with the user before this doc was written; see rationale inline)
**Branch (planned):** `feature/frontend-dashboard`, stacked on `feature/api-tests`

## 1. Purpose

Four polish/feature requests on the existing Streamlit frontend (`docs/superpowers/specs/2026-08-17-streamlit-frontend-design.md`):

1. Move `top_k`/`score_threshold` out of the main Ask flow into a Settings popover.
2. Prefix citation chips with "Source: " instead of the bare filename.
3. A new **Dashboard** tab showing real ingestion timing (pulled from Inngest) and Ask-side response timing.
4. Hide Streamlit's default "Deploy" button (framework chrome, irrelevant to this app).

## 2. Settings popover

Replace the inline `st.columns(2)` sliders in the Ask tab with `st.popover("⚙️ Settings")`, placed between the question input and the Ask button. Same two sliders, same bounds/defaults (`top_k`: 1–20, default 5; `score_threshold`: 0.0–1.0, default 0.5, step 0.05) — purely a layout change, no behavior change to `api_client.ask(...)`'s call.

## 3. Citation chip label

`render_citation_chips` (in `streamlit_app.py`) changes its chip text from `{source}` to `Source: {source}`, still HTML-escaped, still one chip per cited source.

## 4. Dashboard tab

### 4.1 Why this needs a new backend endpoint

The existing `GET /ingest/{event_id}/status` only returns `{event_id, status, ingested, error}` — no timing breakdown. Streamlit's UI can't get step-level Inngest timing from anywhere it currently calls.

**`POST /ask` never touches Inngest at all** (Decision D5) — there is no Inngest run to query for Ask timing under any circumstance. Ask-side timing is measured client-side instead (§4.4).

### 4.2 Where the Inngest step-timing data actually comes from

Investigated and confirmed live against the running Inngest Dev Server (not assumed from docs alone): the Dev Server exposes a local GraphQL endpoint at `{INNGEST_API_ORIGIN}v0/gql` with a `runTrace(runID: String!)` query. Confirmed via introspection and a real query against a live ingest run:

```graphql
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
```

Real response observed for a completed ingest run (`run.duration` = total run time in ms; each `childrenSpans` entry = one Inngest step):

```json
{
  "data": {
    "runTrace": {
      "duration": 1060,
      "childrenSpans": [
        {"name": "Load and Chunk pdf", "duration": null, ...},
        {"name": "Load and Chunk pdf", "duration": 13, ...},
        {"name": "Embedding and upsert", "duration": null, ...},
        {"name": "Embedding and upsert", "duration": 788, ...},
        {"name": "Finalization", "duration": null, ...},
        {"name": "executor.nonstep", "duration": 10, ...}
      ]
    }
  }
}
```

Two things the backend must handle, confirmed from this real response, not assumed:
- **Duplicate entries per step name** — Inngest emits a scheduling-marker span (`duration: null`) and an execution span (`duration: <real ms>`) per step. Dedupe by `name`, keeping whichever entry has a non-null `duration`.
- **Internal bookkeeping spans** (`executor.nonstep` and anything else prefixed `executor.`) are not user-meaningful and must be filtered out.

**⚠️ Known limitation, stated plainly:** this is the Inngest **Dev Server's** local API. Whether Inngest Cloud exposes the same GraphQL endpoint/shape was not confirmed (out of scope to verify — this whole project is local-only, no deployment yet). If this project is ever pointed at Inngest Cloud instead of the local Dev Server, the Dashboard tab's trace fetch should be expected to need rework, and that's an acceptable, explicitly-flagged trade-off for now — not a silent gap.

### 4.3 New backend endpoint — `GET /ingest/{event_id}/trace`

Added to `routers.py`, alongside `ingest`/`ingest_status`.

**Process:**
1. Look up `run_id` the same way `ingest_status` already does — `GET {api_origin}v1/events/{event_id}/runs`, take `runs[0]["run_id"]`. If no runs yet, respond `200` with an empty/no-data trace (mirrors `ingest_status`'s existing "no runs yet" handling — not an error).
2. `POST {api_origin}v0/gql` with the `runTrace` query above, `variables: {"runID": run_id}`.
3. Parse `childrenSpans`: skip any span whose `name` starts with `"executor."`; dedupe by `name`, preferring a non-null `duration`; if a span's `duration` is still null after dedup, fall back to computing it from `endedAt - startedAt` (ISO 8601 timestamps, both already present on every observed span).
4. Relabel each step's raw Inngest name for a normal user, via a fixed mapping (fallback: use the raw name if not mapped, so an unrecognized future step name doesn't disappear or crash):
   - `"Load and Chunk pdf"` → `"Reading & splitting the document"`
   - `"Embedding and upsert"` → `"Generating embeddings & saving to the index"`
   - `"Finalization"` → `"Finishing up"`
5. Return `{event_id, total_duration_ms, steps: [{label, duration_ms}, ...]}` in step order as Inngest returned them (already chronological).

**New Pydantic models** (`custom_types.py`): `TraceStep(label: str, duration_ms: int | None)`, `IngestTraceResponse(event_id: str, total_duration_ms: int | None, steps: list[TraceStep])`.

**Errors:** same convention as `ingest_status` — `httpx.RequestError` (either HTTP call) → `503`; a non-2xx response from either Inngest call → `502`. No new error classes.

### 4.4 Ask-side timing (client-side, no backend change)

In the Ask tab, wrap the existing `api_client.ask(...)` call with `time.perf_counter()` before/after; store the elapsed seconds in `st.session_state["last_ask_duration"]` right alongside the existing result-handling code. No new backend endpoint — this is purely "how long did the HTTP round-trip actually take," measured where the call is made.

### 4.5 Frontend — fetch-once-cache pattern, new Dashboard tab

**`api_client.get_trace(event_id: str) -> IngestTraceResponse`** — same shape as `ask`/`ingest`/`check_status`: one `_request("GET", f"/ingest/{event_id}/trace")` call, parsed into `IngestTraceResponse`.

**Upload tab change:** once the existing poll loop reaches `status.status == "Completed"`, additionally call `api_client.get_trace(event_id)` once and store the result in `st.session_state["ingest_trace"]` (wrapped in the same `ApiError` handling pattern already used elsewhere — on failure, store `None` rather than raising, since trace data is a nice-to-have and shouldn't block the upload flow's own success message). This fetch happens **once**, right after the poll loop concludes — not on every Streamlit rerun — matching the project's established lesson from the earlier poll-loop bug (Session 12): don't do unbounded work gated on persistent state that survives every rerun.

**New `dashboard_tab`** (third tab: `ask_tab, upload_tab, dashboard_tab = st.tabs([...])`), pure rendering, reads only `st.session_state`, makes no API calls of its own:

- **Section "Document Processing"**: if `st.session_state.get("ingest_trace")` is set, show total time (`st.metric` or similar) and each step with its humanized label + duration (ms for &lt;1000ms, seconds with 2 decimals otherwise — matches the mixed-unit style the user's own example showed, e.g. "129ms" vs. "10.63s"). If not set, an empty-state message: "Upload a PDF to see processing stats here."
- **Section "Answering"**: if `st.session_state.get("last_ask_duration")` is set, show "Answered in X.XXs". Else: "Ask a question to see timing here."

## 5. Hide the Deploy button

`.streamlit/config.toml` gains a `[client]` section:
```toml
[client]
toolbarMode = "viewer"
```
Confirmed via current Streamlit docs: `"viewer"` hides developer-only toolbar items (including Deploy) for all viewers. Additive to the existing `[theme]` section already in that file.

## 6. Testing

- Backend: `GET /ingest/{event_id}/trace` gets real unit tests via TDD (mocked `httpx`, same pattern as `test_ingest_endpoint.py`) — covering: no-runs-yet → empty trace; a real trace response with duplicate/null-duration spans → correctly deduped and relabeled; `executor.*` spans filtered out; a step name not in the label map falls back to its raw name; connection failure → 503; upstream error → 502.
- `api_client.get_trace` gets a unit test matching the existing `ask`/`ingest`/`check_status` pattern in `test_api_client.py`.
- `streamlit_app.py` changes (Settings popover, citation label, Dashboard tab, Ask timing) get no automated tests — same established, spec-authorized boundary as the rest of the UI (verified live/manually instead).

## 7. Out of scope (explicitly)

- A history of past uploads/questions (Dashboard shows only the most recent of each — a list view is a bigger feature, not requested).
- Any change to Inngest Cloud compatibility for the trace endpoint (§4.2's flagged limitation).
- Auto-refreshing the Dashboard tab while an ingest is still running (it's populated once, after the existing poll loop already completes).
