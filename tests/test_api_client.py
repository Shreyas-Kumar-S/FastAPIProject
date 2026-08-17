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
