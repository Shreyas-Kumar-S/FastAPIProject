from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from fastapiproject import data_loader, main, vector_db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(vector_db, "_storage_singleton", None)
    with TestClient(main.app) as c:
        yield c


def test_ask_wires_through_the_real_service_layer_end_to_end(client):
    """Unlike test_ask_endpoint.py (which stubs rag_service.answer_question
    directly at the router boundary), this exercises the full real chain:
    routers.ask -> rag_service.answer_question -> search_context ->
    build_prompt -> generate_answer -> parse_cited_answer -> QueryResult.
    Only the actual external network clients (Qdrant, Gemini) are mocked."""
    fake_qdrant_client = AsyncMock()
    fake_qdrant_client.collection_exists.return_value = True
    point = MagicMock()
    point.payload = {"source": "doc1.pdf", "text": "Cats are mammals."}
    fake_qdrant_client.query_points.return_value = MagicMock(points=[point])

    fake_embed_response = MagicMock()
    fake_embed_response.embeddings = [MagicMock(values=[0.1, 0.2])]

    fake_generate_response = MagicMock()
    fake_generate_response.text = "Cats are mammals.\nUsed: [1]"

    with patch.object(vector_db, "AsyncQdrantClient", return_value=fake_qdrant_client), \
         patch.object(data_loader.client.aio.models, "embed_content", AsyncMock(return_value=fake_embed_response)), \
         patch.object(data_loader.client.aio.models, "generate_content", AsyncMock(return_value=fake_generate_response)):
        response = client.post("/ask", json={"question": "What are cats?"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"answers": "Cats are mammals.", "sources": ["doc1.pdf"], "num_contexts": 1}
    fake_qdrant_client.query_points.assert_called_once()


def test_ask_wires_through_empty_context_short_circuit_end_to_end(client):
    """Same real chain as above, but Qdrant returns no matches - proves F8's
    short-circuit (rag_service.answer_question) is reached without ever
    calling Gemini's generation endpoint, through the real HTTP layer."""
    fake_qdrant_client = AsyncMock()
    fake_qdrant_client.collection_exists.return_value = True
    fake_qdrant_client.query_points.return_value = MagicMock(points=[])

    fake_embed_response = MagicMock()
    fake_embed_response.embeddings = [MagicMock(values=[0.1, 0.2])]

    with patch.object(vector_db, "AsyncQdrantClient", return_value=fake_qdrant_client), \
         patch.object(data_loader.client.aio.models, "embed_content", AsyncMock(return_value=fake_embed_response)), \
         patch.object(data_loader.client.aio.models, "generate_content", AsyncMock()) as mock_generate:
        response = client.post("/ask", json={"question": "What are cats?"})

    assert response.status_code == 200
    body = response.json()
    assert body["num_contexts"] == 0
    assert body["sources"] == []
    mock_generate.assert_not_called()
