import re
import uuid

from google.genai import types as genai_types

from fastapiproject.custom_types import PdfRef, QueryResult, RagChunkAndSource, RagSearchResult, RagUpsertResult
from fastapiproject.data_loader import client as genai_client
from fastapiproject.data_loader import embed_texts, load_and_chunk_pdf
from fastapiproject.vector_db import get_storage

SYSTEM_INSTRUCTION = (
    "Use only the context to answer the question. Always end your response "
    "with a 'Used: [n, n, ...]' line citing only the context chunk numbers "
    "you actually relied on."
)

# Matches the model id already used for generation in main.py's rag_query_pdf_ai,
# so /ask's direct call (Decision D5) answers with the same model as the Inngest path.
GENERATION_MODEL = "gemini-3.6-flash"


def load_and_chunk_sources(pdfs: list[PdfRef]) -> RagChunkAndSource:
    all_chunks: list[str] = []
    all_sources: list[str] = []
    for pdf in pdfs:
        source_id = pdf.source_id if pdf.source_id is not None else pdf.pdf_path
        chunks = load_and_chunk_pdf(pdf.pdf_path)
        all_chunks.extend(chunks)
        all_sources.extend([source_id] * len(chunks))
    return RagChunkAndSource(chunks=all_chunks, sources=all_sources)


async def embed_and_upsert(chunks_and_src: RagChunkAndSource) -> RagUpsertResult:
    chunks = chunks_and_src.chunks
    sources = chunks_and_src.sources
    vecs = await embed_texts(chunks)
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sources[i]}:{i}")) for i in range(len(chunks))]
    payloads = [{"source": sources[i], "text": chunks[i]} for i in range(len(chunks))]
    await get_storage().upsert(ids, vecs, payloads)
    return RagUpsertResult(ingested=len(chunks))


async def search_context(question: str, top_k: int = 5, score_threshold: float = 0.5) -> RagSearchResult:
    query_vec = (await embed_texts([question]))[0]
    found = await get_storage().search(query_vec, top_k, score_threshold)
    return RagSearchResult(contexts=found["context"], sources=found["sources"])


def build_prompt(question: str, contexts: list[str]) -> str:
    context_block = "\n\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts))
    return (
        "Use the following numbered context chunks to answer the question:\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n\n"
        "Answer concisely using only the context above. "
        "Then, on a new final line, output exactly: Used: [n, n, ...] "
        "listing the bracket numbers of only the context chunks you actually relied on to answer. "
        "If none were relevant, output Used: []."
    )


NO_CONTEXT_ANSWER = "No relevant context was found to answer this question."


async def answer_question(question: str, top_k: int = 5, score_threshold: float = 0.5) -> QueryResult:
    found = await search_context(question, top_k, score_threshold)
    if not found.contexts:
        return QueryResult(answers=NO_CONTEXT_ANSWER, sources=[], num_contexts=0)
    prompt = build_prompt(question, found.contexts)
    raw_answer = await generate_answer(prompt)
    answer, cited_sources = parse_cited_answer(raw_answer, found.sources)
    return QueryResult(answers=answer, sources=cited_sources, num_contexts=len(found.contexts))


async def generate_answer(prompt: str) -> str:
    response = await genai_client.aio.models.generate_content(
        model=GENERATION_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.3,
            max_output_tokens=1024,
        ),
    )
    return response.text.strip()


def parse_cited_answer(raw_answer: str, sources: list[str]) -> tuple[str, list[str]]:
    used_match = re.search(r"Used:\s*\[([^\]]*)\]\s*$", raw_answer)
    if used_match:
        answer = raw_answer[:used_match.start()].strip()
        used_indices = [int(n) for n in re.findall(r"\d+", used_match.group(1))]
        cited_sources = []
        for i in used_indices:
            if 1 <= i <= len(sources):
                source = sources[i - 1]
                if source not in cited_sources:
                    cited_sources.append(source)
    else:
        answer = raw_answer
        cited_sources = list(dict.fromkeys(sources))
    return answer, cited_sources
