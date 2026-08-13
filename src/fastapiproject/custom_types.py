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

