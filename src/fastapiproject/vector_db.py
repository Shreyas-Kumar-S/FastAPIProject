from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

class QdrantStorage:
    def __init__(self, url="http://localhost:6333", collections = "docs", dims=3072):
        self.client = AsyncQdrantClient(url = url, timeout=30)
        self.collection = collections
        self.dims = dims
        self._collection_ready = False

    async def _ensure_collection(self):
        if self._collection_ready:
            return
        if not await self.client.collection_exists(self.collection):
            await self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dims, distance=Distance.COSINE),
            )
        self._collection_ready = True

    async def upsert(self, ids,vectors,payloads):
        await self._ensure_collection()
        points = [PointStruct(id=ids[i],vector=vectors[i],payload=payloads[i]) for i in range(len(ids))]
        await self.client.upsert(self.collection,points=points)

    async def search(self, query_vector, top_k: int = 5, score_threshold: float = 0.5):
        await self._ensure_collection()
        response = await self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            with_payload=True,
            limit=top_k,
            score_threshold=score_threshold,
        )

        context=[]
        sources = []

        for result in response.points:
            payload = getattr(result,"payload", None) or {}
            text = payload.get("text", "")
            source=payload.get("source", "")
            if text:
                context.append(text)
                sources.append(source)

        return {"context":context,"sources":sources}

# Module-level singleton: callers should go through get_storage() instead of
# constructing QdrantStorage() directly. This means the AsyncQdrantClient is
# built once per process; the collection_exists/create_collection check is
# deferred to the first upsert()/search() call (async I/O can't run in
# __init__) but is likewise only ever done once, via _ensure_collection.
_storage_singleton: QdrantStorage | None = None

def get_storage() -> QdrantStorage:
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = QdrantStorage()
    return _storage_singleton
