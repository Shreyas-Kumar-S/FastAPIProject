import uuid
from pathlib import Path

import httpx
import inngest
from fastapi import APIRouter, File, HTTPException, UploadFile
from qdrant_client.http.exceptions import ApiException as QdrantApiException

from fastapiproject import data_loader, rag_service
from fastapiproject.custom_types import AskRequest, IngestResponse, IngestStatusResponse, QueryResult
from fastapiproject.inngest_client import client as inngest_client

ask_router = APIRouter()
ingest_router = APIRouter()

MAX_PDF_SIZE_BYTES = 20 * 1024 * 1024


def upload_dir() -> Path:
    return data_loader.PDF_UPLOAD_ROOT / "uploads"


def ensure_upload_dir() -> None:
    upload_dir().mkdir(parents=True, exist_ok=True)


@ask_router.post("/ask", response_model=QueryResult)
async def ask(request: AskRequest) -> QueryResult:
    try:
        return await rag_service.answer_question(request.question, request.top_k, request.score_threshold)
    except QdrantApiException as exc:
        raise HTTPException(status_code=503, detail="Vector store is unreachable") from exc


@ingest_router.post("/ingest", response_model=IngestResponse, status_code=202)
async def ingest(files: list[UploadFile] = File(...)) -> IngestResponse:
    if not files:
        raise HTTPException(status_code=422, detail="At least one file is required")

    pdfs = []
    dest_dir = upload_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf") or file.content_type != "application/pdf":
            raise HTTPException(status_code=422, detail=f"Invalid file type: {file.filename}")

        content = await file.read()
        if len(content) > MAX_PDF_SIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large: {file.filename}")

        safe_name = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
        dest_path = dest_dir / safe_name
        dest_path.write_bytes(content)

        relative_path = dest_path.relative_to(data_loader.PDF_UPLOAD_ROOT)
        data_loader.resolve_safe_pdf_path(str(relative_path), base_dir=data_loader.PDF_UPLOAD_ROOT)

        pdfs.append({"pdf_path": str(relative_path), "source_id": file.filename})

    event_ids = await inngest_client.send(inngest.Event(name="rag/ingest_pdf", data={"pdfs": pdfs}))
    event_id = event_ids[0]
    return IngestResponse(event_id=event_id, status_url=f"/ingest/{event_id}/status")


@ingest_router.get("/ingest/{event_id}/status", response_model=IngestStatusResponse)
async def ingest_status(event_id: str) -> IngestStatusResponse:
    url = f"{inngest_client.api_origin}v1/events/{event_id}/runs"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Inngest API is unreachable") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Inngest API returned an error")

    runs = response.json().get("data", [])
    if not runs:
        return IngestStatusResponse(event_id=event_id, status="Queued")

    run = runs[0]
    status = run.get("status", "Unknown")
    ingested = None
    error = None
    if status == "Completed":
        ingested = (run.get("output") or {}).get("ingested")
    elif status == "Failed":
        error = run.get("error")

    return IngestStatusResponse(event_id=event_id, status=status, ingested=ingested, error=error)
