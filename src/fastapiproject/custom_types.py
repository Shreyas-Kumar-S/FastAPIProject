import pydantic

class PdfRef(pydantic.BaseModel):
    pdf_path: str
    source_id: str | None = None

class RagChunkAndSource(pydantic.BaseModel):
    chunks: list[str]
    sources: list[str]

class RagUpsertResult(pydantic.BaseModel):
    ingested : int

class RagSearchResult(pydantic.BaseModel):
    contexts: list[str]
    sources: list[str]

class QueryResult(pydantic.BaseModel):
    answers: str
    sources: list[str]
    num_contexts: int

class AskRequest(pydantic.BaseModel):
    question: str = pydantic.Field(min_length=1)
    top_k: int = pydantic.Field(default=5, ge=1, le=20)
    score_threshold: float = pydantic.Field(default=0.5, ge=0.0, le=1.0)

class IngestResponse(pydantic.BaseModel):
    event_id: str
    status_url: str

class IngestStatusResponse(pydantic.BaseModel):
    event_id: str
    status: str
    ingested: int | None = None
    error: str | None = None

class TraceStep(pydantic.BaseModel):
    label: str
    duration_ms: int | None = None

class IngestTraceResponse(pydantic.BaseModel):
    event_id: str
    total_duration_ms: int | None = None
    steps: list[TraceStep] = []

