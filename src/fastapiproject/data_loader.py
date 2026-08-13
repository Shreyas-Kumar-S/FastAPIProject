import os
from pathlib import Path

from google import genai
from llama_index.readers.file import PDFReader
from llama_index.core.node_parser import SentenceSplitter
from dotenv import load_dotenv

load_dotenv()

client = genai.Client()
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 3072

splitter = SentenceSplitter(chunk_size=1000,chunk_overlap=200)

# Project root: .../FastAPIProject (three levels up from this file).
# Every pdf_path we're asked to read must resolve to somewhere inside this
# directory - blocks path traversal (e.g. "../../etc/passwd") from event
# payloads. Overridable via PDF_UPLOAD_ROOT for deployments that want a
# narrower, dedicated upload directory.
PDF_UPLOAD_ROOT = Path(os.environ.get("PDF_UPLOAD_ROOT", Path(__file__).resolve().parents[2])).resolve()

def resolve_safe_pdf_path(path: str, base_dir: Path = PDF_UPLOAD_ROOT) -> Path:
    candidate = (base_dir / path).resolve()
    if not candidate.is_relative_to(base_dir):
        raise ValueError(f"pdf_path '{path}' resolves outside the allowed directory ({base_dir})")
    if not candidate.is_file():
        raise FileNotFoundError(f"No such PDF file: {candidate}")
    return candidate

def load_and_chunk_pdf(path:str):
    safe_path = resolve_safe_pdf_path(path)
    docs = PDFReader().load_data(file=safe_path)
    texts = [doc.text for doc in docs if getattr(doc,"text",None)]
    chunks = []
    for t in texts:
        chunks.extend(splitter.split_text(t))
    return chunks

def embed_texts(texts: list[str]) -> list[list[float]]:
    response = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
    )
    embeddings = response.embeddings or []
    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"Embedding count mismatch: requested {len(texts)} embeddings, "
            f"got {len(embeddings)}. Refusing to return misaligned vectors."
        )
    vectors: list[list[float]] = []
    for i, item in enumerate(embeddings):
        if item.values is None:
            raise RuntimeError(f"Embedding at index {i} returned no values")
        vectors.append(item.values)
    return vectors
