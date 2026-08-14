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

