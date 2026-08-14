from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException

from fastapiproject import main, rag_service
from fastapiproject.custom_types import QueryResult


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    with TestClient(main.app) as c:
        yield c


def test_ask_returns_200_with_answer(client):
    fake_result = QueryResult(answers="the answer", sources=["doc1"], num_contexts=2)

    with patch.object(rag_service, "answer_question", return_value=fake_result) as mock_answer:
        response = client.post("/ask", json={"question": "what is x?"})

    mock_answer.assert_called_once_with("what is x?", 5, 0.5)
    assert response.status_code == 200
    assert response.json() == {"answers": "the answer", "sources": ["doc1"], "num_contexts": 2}


def test_ask_passes_through_custom_top_k_and_score_threshold(client):
    fake_result = QueryResult(answers="a", sources=[], num_contexts=0)

    with patch.object(rag_service, "answer_question", return_value=fake_result) as mock_answer:
        client.post("/ask", json={"question": "q", "top_k": 3, "score_threshold": 0.9})

    mock_answer.assert_called_once_with("q", 3, 0.9)


def test_ask_rejects_empty_question(client):
    response = client.post("/ask", json={"question": ""})

    assert response.status_code == 422


def test_ask_rejects_top_k_out_of_bounds(client):
    response = client.post("/ask", json={"question": "q", "top_k": 21})

    assert response.status_code == 422


def test_ask_maps_qdrant_unreachable_to_503(client):
    with patch.object(rag_service, "answer_question", side_effect=ResponseHandlingException(ConnectionError())):
        response = client.post("/ask", json={"question": "q"})

    assert response.status_code == 503
