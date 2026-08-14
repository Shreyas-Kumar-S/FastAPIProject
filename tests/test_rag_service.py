from unittest.mock import AsyncMock, MagicMock, patch

from fastapiproject import rag_service
from fastapiproject.custom_types import PdfRef, RagChunkAndSource, RagSearchResult


def test_load_and_chunk_sources_defaults_source_id_to_pdf_path():
    with patch.object(rag_service, "load_and_chunk_pdf", return_value=["chunk one", "chunk two"]) as mock_load:
        result = rag_service.load_and_chunk_sources([PdfRef(pdf_path="a.pdf")])

    mock_load.assert_called_once_with("a.pdf")
    assert result.chunks == ["chunk one", "chunk two"]
    assert result.sources == ["a.pdf", "a.pdf"]


def test_load_and_chunk_sources_uses_explicit_source_id():
    with patch.object(rag_service, "load_and_chunk_pdf", return_value=["chunk"]):
        result = rag_service.load_and_chunk_sources([PdfRef(pdf_path="a.pdf", source_id="my-doc")])

    assert result.sources == ["my-doc"]


def test_load_and_chunk_sources_combines_multiple_pdfs():
    def fake_load(path):
        return [f"{path}-chunk"]

    with patch.object(rag_service, "load_and_chunk_pdf", side_effect=fake_load):
        result = rag_service.load_and_chunk_sources(
            [PdfRef(pdf_path="a.pdf"), PdfRef(pdf_path="b.pdf")]
        )

    assert result.chunks == ["a.pdf-chunk", "b.pdf-chunk"]
    assert result.sources == ["a.pdf", "b.pdf"]


async def test_embed_and_upsert_builds_deterministic_ids_and_upserts():
    chunks_and_src = RagChunkAndSource(chunks=["hello", "world"], sources=["doc1", "doc1"])
    fake_vecs = [[0.1, 0.2], [0.3, 0.4]]
    fake_store = MagicMock()
    fake_store.upsert = AsyncMock()

    with patch.object(rag_service, "embed_texts", AsyncMock(return_value=fake_vecs)), \
         patch.object(rag_service, "get_storage", return_value=fake_store):
        result = await rag_service.embed_and_upsert(chunks_and_src)

    assert result.ingested == 2
    fake_store.upsert.assert_called_once()
    ids, vecs, payloads = fake_store.upsert.call_args[0]
    assert vecs == fake_vecs
    assert payloads == [
        {"source": "doc1", "text": "hello"},
        {"source": "doc1", "text": "world"},
    ]
    assert len(ids) == 2 and ids[0] != ids[1]


async def test_search_context_returns_contexts_and_sources():
    fake_store = MagicMock()
    fake_store.search = AsyncMock(return_value={"context": ["chunk a"], "sources": ["doc1"]})

    with patch.object(rag_service, "embed_texts", AsyncMock(return_value=[[0.1, 0.2]])), \
         patch.object(rag_service, "get_storage", return_value=fake_store):
        result = await rag_service.search_context("what is x?", top_k=3, score_threshold=0.4)

    fake_store.search.assert_called_once_with([0.1, 0.2], 3, 0.4)
    assert result.contexts == ["chunk a"]
    assert result.sources == ["doc1"]


def test_build_prompt_numbers_contexts_and_includes_question():
    prompt = rag_service.build_prompt("What is RAG?", ["chunk one", "chunk two"])

    assert "[1] chunk one" in prompt
    assert "[2] chunk two" in prompt
    assert "Question: What is RAG?" in prompt
    assert "Used: [n, n, ...]" in prompt


def test_parse_cited_answer_extracts_answer_and_cited_sources():
    raw = "This is the answer.\nUsed: [1, 2]"

    answer, cited = rag_service.parse_cited_answer(raw, ["doc1", "doc2", "doc3"])

    assert answer == "This is the answer."
    assert cited == ["doc1", "doc2"]


def test_parse_cited_answer_handles_empty_used_list():
    raw = "No relevant info.\nUsed: []"

    answer, cited = rag_service.parse_cited_answer(raw, ["doc1"])

    assert answer == "No relevant info."
    assert cited == []


def test_parse_cited_answer_falls_back_when_no_used_line():
    raw = "An answer with no citation line."

    answer, cited = rag_service.parse_cited_answer(raw, ["doc1", "doc1", "doc2"])

    assert answer == raw
    assert cited == ["doc1", "doc2"]


async def test_generate_answer_calls_gemini_and_returns_stripped_text():
    fake_response = MagicMock()
    fake_response.text = "  the answer  \nUsed: [1]  "
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with patch.object(rag_service, "genai_client", fake_client):
        result = await rag_service.generate_answer("some prompt")

    assert result == "the answer  \nUsed: [1]"
    kwargs = fake_client.aio.models.generate_content.call_args.kwargs
    assert kwargs["contents"] == "some prompt"
    assert kwargs["config"].system_instruction == rag_service.SYSTEM_INSTRUCTION


async def test_answer_question_short_circuits_when_no_context_found():
    empty_result = RagSearchResult(contexts=[], sources=[])

    with patch.object(rag_service, "search_context", AsyncMock(return_value=empty_result)) as mock_search, \
         patch.object(rag_service, "generate_answer", AsyncMock()) as mock_generate:
        result = await rag_service.answer_question("what is x?", top_k=3, score_threshold=0.4)

    mock_search.assert_called_once_with("what is x?", 3, 0.4)
    mock_generate.assert_not_called()
    assert result.sources == []
    assert result.num_contexts == 0
    assert result.answers == rag_service.NO_CONTEXT_ANSWER


async def test_answer_question_builds_prompt_and_parses_citation_when_context_found():
    found = RagSearchResult(contexts=["chunk one", "chunk two"], sources=["doc1", "doc2"])

    with patch.object(rag_service, "search_context", AsyncMock(return_value=found)), \
         patch.object(rag_service, "generate_answer", AsyncMock(return_value="the answer\nUsed: [1]")) as mock_generate:
        result = await rag_service.answer_question("what is x?")

    mock_generate.assert_called_once()
    prompt_arg = mock_generate.call_args.args[0]
    assert "chunk one" in prompt_arg and "chunk two" in prompt_arg
    assert result.answers == "the answer"
    assert result.sources == ["doc1"]
    assert result.num_contexts == 2
