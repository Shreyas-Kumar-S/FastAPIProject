from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

class QdrantStorage:
    def __init__(self, url="http://localhost:6333", collections = "docs", dims=3072):
        self.client = QdrantClient(url = url, timeout=30)
        self.collection = collections
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dims,distance=Distance.COSINE),
            )

    def upsert(self, ids,vectors,payloads):
        points = [PointStruct(id=ids[i],vector=vectors[i],payload=payloads[i]) for i in range(len(ids))]
        self.client.upsert(self.collection,points=points)

    def search(self, query_vector, top_k: int = 5, score_threshold: float = 0.5):
        response = self.client.query_points(
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