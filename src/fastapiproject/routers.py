from fastapi import APIRouter, HTTPException
from qdrant_client.http.exceptions import ApiException as QdrantApiException

from fastapiproject import rag_service
from fastapiproject.custom_types import AskRequest, QueryResult

ask_router = APIRouter()


@ask_router.post("/ask", response_model=QueryResult)
async def ask(request: AskRequest) -> QueryResult:
    try:
        return await rag_service.answer_question(request.question, request.top_k, request.score_threshold)
    except QdrantApiException as exc:
        raise HTTPException(status_code=503, detail="Vector store is unreachable") from exc
