from unittest.mock import AsyncMock, MagicMock, patch

from fastapiproject import vector_db


def test_get_storage_constructs_only_once(monkeypatch):
    monkeypatch.setattr(vector_db, "_storage_singleton", None)
    with patch.object(vector_db, "QdrantStorage") as mock_cls:
        mock_cls.return_value = MagicMock()
        first = vector_db.get_storage()
        second = vector_db.get_storage()

    assert first is second
    mock_cls.assert_called_once()


def test_get_storage_reuses_existing_singleton(monkeypatch):
    sentinel = MagicMock()
    monkeypatch.setattr(vector_db, "_storage_singleton", sentinel)
    with patch.object(vector_db, "QdrantStorage") as mock_cls:
        result = vector_db.get_storage()

    assert result is sentinel
    mock_cls.assert_not_called()


def _make_storage_with_fake_client():
    with patch.object(vector_db, "AsyncQdrantClient") as mock_cls:
        fake_client = AsyncMock()
        mock_cls.return_value = fake_client
        storage = vector_db.QdrantStorage()
    return storage, fake_client


async def test_upsert_creates_collection_when_missing_then_upserts():
    storage, fake_client = _make_storage_with_fake_client()
    fake_client.collection_exists.return_value = False

    await storage.upsert(["id1"], [[0.1, 0.2]], [{"source": "doc1", "text": "hello"}])

    fake_client.create_collection.assert_called_once()
    fake_client.upsert.assert_called_once()


async def test_upsert_skips_create_collection_when_already_exists():
    storage, fake_client = _make_storage_with_fake_client()
    fake_client.collection_exists.return_value = True

    await storage.upsert(["id1"], [[0.1, 0.2]], [{"source": "doc1", "text": "hello"}])

    fake_client.create_collection.assert_not_called()
    fake_client.upsert.assert_called_once()


async def test_upsert_only_checks_collection_existence_once_across_calls():
    storage, fake_client = _make_storage_with_fake_client()
    fake_client.collection_exists.return_value = True

    await storage.upsert(["id1"], [[0.1, 0.2]], [{"source": "doc1", "text": "hello"}])
    await storage.upsert(["id2"], [[0.3, 0.4]], [{"source": "doc1", "text": "world"}])

    fake_client.collection_exists.assert_called_once()


async def test_search_returns_context_and_sources_from_matched_points():
    storage, fake_client = _make_storage_with_fake_client()
    fake_client.collection_exists.return_value = True
    point = MagicMock()
    point.payload = {"source": "doc1", "text": "matched chunk"}
    response = MagicMock()
    response.points = [point]
    fake_client.query_points.return_value = response

    result = await storage.search([0.1, 0.2], top_k=3, score_threshold=0.4)

    fake_client.query_points.assert_called_once_with(
        collection_name=storage.collection,
        query=[0.1, 0.2],
        with_payload=True,
        limit=3,
        score_threshold=0.4,
    )
    assert result == {"context": ["matched chunk"], "sources": ["doc1"]}
