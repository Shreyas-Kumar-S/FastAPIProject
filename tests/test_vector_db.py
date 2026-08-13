from unittest.mock import MagicMock, patch

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
