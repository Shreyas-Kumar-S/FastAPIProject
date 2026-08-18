import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from fastapiproject import data_loader, main
from fastapiproject.inngest_client import client as inngest_client


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(data_loader, "PDF_UPLOAD_ROOT", tmp_path)
    with TestClient(main.app) as c:
        yield c


def test_ingest_rejects_empty_file_list(client):
    response = client.post("/ingest", files=[])

    assert response.status_code == 422


def test_ingest_rejects_non_pdf_content_type(client):
    response = client.post("/ingest", files={"files": ("doc.txt", io.BytesIO(b"hello"), "text/plain")})

    assert response.status_code == 422


def test_ingest_rejects_oversized_file(client):
    big_content = b"x" * (20 * 1024 * 1024 + 1)

    response = client.post("/ingest", files={"files": ("big.pdf", io.BytesIO(big_content), "application/pdf")})

    assert response.status_code == 413


def test_ingest_saves_file_sends_event_and_returns_202(client, tmp_path):
    with patch.object(inngest_client, "send", AsyncMock(return_value=["evt_123"])) as mock_send:
        response = client.post(
            "/ingest", files={"files": ("doc.pdf", io.BytesIO(b"%PDF-1.4 hello"), "application/pdf")}
        )

    assert response.status_code == 202
    body = response.json()
    assert body == {"event_id": "evt_123", "status_url": "/ingest/evt_123/status"}

    mock_send.assert_called_once()
    event = mock_send.call_args.args[0]
    assert event.data["pdfs"][0]["source_id"] == "doc.pdf"

    saved_files = list((tmp_path / "uploads").iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == b"%PDF-1.4 hello"


def test_ingest_sanitizes_path_traversal_in_filename(client, tmp_path):
    with patch.object(inngest_client, "send", AsyncMock(return_value=["evt_1"])):
        client.post("/ingest", files={"files": ("../../evil.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})

    saved_files = list((tmp_path / "uploads").iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].name.endswith("_evil.pdf")
    assert not (tmp_path / "evil.pdf").exists()


def test_ingest_accepts_multiple_files_in_one_request(client, tmp_path):
    with patch.object(inngest_client, "send", AsyncMock(return_value=["evt_multi"])) as mock_send:
        response = client.post(
            "/ingest",
            files=[
                ("files", ("a.pdf", io.BytesIO(b"%PDF-1.4 a"), "application/pdf")),
                ("files", ("b.pdf", io.BytesIO(b"%PDF-1.4 b"), "application/pdf")),
            ],
        )

    assert response.status_code == 202
    event = mock_send.call_args.args[0]
    assert [pdf["source_id"] for pdf in event.data["pdfs"]] == ["a.pdf", "b.pdf"]

    saved_files = list((tmp_path / "uploads").iterdir())
    assert len(saved_files) == 2


def test_ingest_rejects_file_with_no_filename(client):
    response = client.post("/ingest", files={"files": ("", io.BytesIO(b"%PDF-1.4"), "application/pdf")})

    assert response.status_code == 422


def test_ingest_returns_500_when_inngest_send_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(data_loader, "PDF_UPLOAD_ROOT", tmp_path)
    with TestClient(main.app, raise_server_exceptions=False) as no_raise_client:
        with patch.object(inngest_client, "send", AsyncMock(side_effect=RuntimeError("boom"))):
            response = no_raise_client.post(
                "/ingest", files={"files": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")}
            )

    assert response.status_code == 500


def test_status_returns_queued_when_no_runs_yet(client):
    fake_response = httpx.Response(200, json={"data": []})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/status")

    assert response.status_code == 200
    assert response.json() == {"event_id": "evt_1", "status": "Queued", "ingested": None, "error": None}


def test_status_returns_completed_with_ingested_count(client):
    fake_response = httpx.Response(200, json={"data": [{"status": "Completed", "output": {"ingested": 5}}]})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/status")

    body = response.json()
    assert body["status"] == "Completed"
    assert body["ingested"] == 5


def test_status_reports_running_when_completed_but_output_not_yet_attached(client):
    # Inngest's Dev Server can briefly cache a "Completed" run snapshot taken
    # before the output field was attached (~15-20s server-side cache);
    # every request in that window gets status=Completed with no output.
    fake_response = httpx.Response(200, json={"data": [{"status": "Completed"}]})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/status")

    body = response.json()
    assert body["status"] == "Running"
    assert body["ingested"] is None


def test_status_returns_failed_with_error(client):
    fake_response = httpx.Response(200, json={"data": [{"status": "Failed", "error": "boom"}]})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/status")

    body = response.json()
    assert body["status"] == "Failed"
    assert body["error"] == "boom"


def test_status_maps_connection_failure_to_503(client):
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("nope"))):
        response = client.get("/ingest/evt_1/status")

    assert response.status_code == 503


def test_status_maps_upstream_error_to_502(client):
    fake_response = httpx.Response(500, json={})
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=fake_response)):
        response = client.get("/ingest/evt_1/status")

    assert response.status_code == 502


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
