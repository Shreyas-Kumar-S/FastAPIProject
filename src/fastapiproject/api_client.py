import os

import httpx

from fastapiproject.custom_types import IngestResponse, IngestStatusResponse, QueryResult

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")


class ApiError(Exception):
    def __init__(self, status_code: int | None, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"{BACKEND_URL}{path}"
    kwargs.setdefault("timeout", 120)
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


def ingest(files: list[tuple[str, bytes]]) -> IngestResponse:
    multipart_files = [("files", (name, content, "application/pdf")) for name, content in files]
    response = _request("POST", "/ingest", files=multipart_files)
    return IngestResponse(**response.json())


def check_status(event_id: str) -> IngestStatusResponse:
    response = _request("GET", f"/ingest/{event_id}/status")
    return IngestStatusResponse(**response.json())
