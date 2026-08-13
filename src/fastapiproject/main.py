import logging
import re
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
import inngest
import inngest.fast_api
from  inngest.experimental import ai
from inngest.experimental.ai import gemini
from google import genai
import uuid
import os
import datetime
from fastapiproject.custom_types import RagChunkAndSource,QueryResult,RagSearchResult,RagUpsertResult
from fastapiproject.data_loader import load_and_chunk_pdf, embed_texts
from fastapiproject.vector_db import QdrantStorage

load_dotenv()

REQUIRED_ENV_VARS = ["GEMINI_API_KEY"]

def _validate_required_env_vars() -> None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            f"{', '.join(missing)}. Set them in your .env file or the "
            "process environment before starting the app."
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_required_env_vars()
    yield

inngest_client = inngest.Inngest(
    app_id="rag_app",
    logger=logging.getLogger("uvicorn"),
    is_production=False,
    serializer=inngest.PydanticSerializer()
)

@inngest_client.create_function(
    fn_id="rag-ingest-pdf",
    name="RAG: Ingest Pdf",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf")
)

async def rag_ingest_pdf(ctx: inngest.Context):

    def _load(ctx: inngest.Context) -> RagChunkAndSource:
        pdfs = ctx.event.data["pdfs"]
        all_chunks: list[str] = []
        all_sources: list[str] = []
        for pdf in pdfs:
            pdf_path = pdf["pdf_path"]
            source_id = pdf.get("source_id", pdf_path)
            chunks = load_and_chunk_pdf(pdf_path)
            all_chunks.extend(chunks)
            all_sources.extend([source_id] * len(chunks))
        return RagChunkAndSource(chunks=all_chunks,sources=all_sources)


    def _upsert(chunks_and_src : RagChunkAndSource) -> RagUpsertResult:
        chunks = chunks_and_src.chunks
        sources = chunks_and_src.sources
        vecs = embed_texts(chunks)
        ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sources[i]}:{i}")) for i in range(len(chunks))]
        payloads = [{"source":sources[i],"text": chunks[i]} for i in range(len(chunks))]
        QdrantStorage().upsert(ids,vecs,payloads)
        return RagUpsertResult(ingested=len(chunks))



    chunks_and_src = await ctx.step.run("Load and Chunk pdf",lambda : _load(ctx), output_type=RagChunkAndSource)
    ingested = await ctx.step.run("Embedding and upsert", lambda : _upsert(chunks_and_src), output_type=RagUpsertResult)

    return ingested.model_dump()

@inngest_client.create_function(
    fn_id="RAG: Query pdf",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf_ai")
)

async def rag_query_pdf_ai(ctx: inngest.Context):
    def _search(question:str, top_k: int = 5, score_threshold: float = 0.5) -> RagSearchResult:
        query_vec = embed_texts([question])[0]
        store = QdrantStorage()
        found = store.search(query_vec, top_k, score_threshold)
        return  RagSearchResult(contexts=found["context"],sources=found["sources"])

    question = ctx.event.data["question"]
    top_k = int(ctx.event.data.get("top_k",5))
    score_threshold = float(ctx.event.data.get("score_threshold",0.5))

    found = await ctx.step.run("searching", lambda : _search(question,top_k,score_threshold), output_type=RagSearchResult)

    context_block = "\n\n".join(f"[{i+1}] {c}" for i, c in enumerate(found.contexts))

    user_content = (
        "Use the following numbered context chunks to answer the question:\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n\n"
        "Answer concisely using only the context above. "
        "Then, on a new final line, output exactly: Used: [n, n, ...] "
        "listing the bracket numbers of only the context chunks you actually relied on to answer. "
        "If none were relevant, output Used: []."
    )

    adapter = gemini.Adapter(
        auth_key=os.environ["GEMINI_API_KEY"],
        model="gemini-3.6-flash"
    )
    res = await ctx.step.ai.infer(
        "llm-answer",
        adapter=adapter,
        body={
            "system_instruction": {
                "parts": [{"text": "Use only the context to answer the question. Always end your response with a 'Used: [n, n, ...]' line citing only the context chunk numbers you actually relied on."}],
            },
            "contents": [
                {"role": "user", "parts": [{"text": user_content}]},
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1024,
            },
        }
    )

    raw_answer = res["candidates"][0]["content"]["parts"][0]["text"].strip()

    used_match = re.search(r"Used:\s*\[([^\]]*)\]\s*$", raw_answer)
    if used_match:
        answer = raw_answer[:used_match.start()].strip()
        used_indices = [int(n) for n in re.findall(r"\d+", used_match.group(1))]
        cited_sources = []
        for i in used_indices:
            if 1 <= i <= len(found.sources):
                source = found.sources[i - 1]
                if source not in cited_sources:
                    cited_sources.append(source)
    else:
        answer = raw_answer
        cited_sources = list(dict.fromkeys(found.sources))

    return {"answers":answer,"sources": cited_sources, "num_contexts": len(found.contexts)}
app = FastAPI(lifespan=lifespan)

inngest.fast_api.serve(app,inngest_client,[rag_ingest_pdf, rag_query_pdf_ai])