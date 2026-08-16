import logging
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
import inngest
import inngest.fast_api
from  inngest.experimental import ai
from inngest.experimental.ai import gemini
from google import genai
import os
import datetime
from fastapiproject import rag_service
from fastapiproject.custom_types import PdfRef,RagChunkAndSource,QueryResult,RagSearchResult,RagUpsertResult
from fastapiproject.inngest_client import client as inngest_client
from fastapiproject.routers import ask_router, ensure_upload_dir, ingest_router

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
    ensure_upload_dir()
    yield

@inngest_client.create_function(
    fn_id="rag-ingest-pdf",
    name="RAG: Ingest Pdf",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf")
)

async def rag_ingest_pdf(ctx: inngest.Context):

    def _load(ctx: inngest.Context) -> RagChunkAndSource:
        pdfs = [PdfRef(**pdf) for pdf in ctx.event.data["pdfs"]]
        return rag_service.load_and_chunk_sources(pdfs)

    async def _upsert(chunks_and_src : RagChunkAndSource) -> RagUpsertResult:
        return await rag_service.embed_and_upsert(chunks_and_src)

    chunks_and_src = await ctx.step.run("Load and Chunk pdf",lambda : _load(ctx), output_type=RagChunkAndSource)
    ingested = await ctx.step.run("Embedding and upsert", lambda : _upsert(chunks_and_src), output_type=RagUpsertResult)

    return ingested.model_dump()

@inngest_client.create_function(
    fn_id="RAG: Query pdf",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf_ai")
)

async def rag_query_pdf_ai(ctx: inngest.Context):
    async def _search(question:str, top_k: int = 5, score_threshold: float = 0.5) -> RagSearchResult:
        return await rag_service.search_context(question, top_k, score_threshold)

    question = ctx.event.data["question"]
    top_k = int(ctx.event.data.get("top_k",5))
    score_threshold = float(ctx.event.data.get("score_threshold",0.5))

    found = await ctx.step.run("searching", lambda : _search(question,top_k,score_threshold), output_type=RagSearchResult)

    user_content = rag_service.build_prompt(question, found.contexts)
    adapter = gemini.Adapter(
        auth_key=os.environ["GEMINI_API_KEY"],
        model="gemini-3.6-flash"
    )
    res = await ctx.step.ai.infer(
        "llm-answer",
        adapter=adapter,
        body={
            "system_instruction": {
                "parts": [{"text": rag_service.SYSTEM_INSTRUCTION}],
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
    answer, cited_sources = rag_service.parse_cited_answer(raw_answer, found.sources)

    return {"answers":answer,"sources": cited_sources, "num_contexts": len(found.contexts)}
app = FastAPI(lifespan=lifespan)
app.include_router(ask_router)
app.include_router(ingest_router)

inngest.fast_api.serve(app,inngest_client,[rag_ingest_pdf, rag_query_pdf_ai])