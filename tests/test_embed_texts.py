from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from fastapiproject import data_loader


def _fake_response(embeddings):
    return SimpleNamespace(embeddings=embeddings)


async def test_embed_texts_returns_one_vector_per_input():
    fake = _fake_response([SimpleNamespace(values=[0.1, 0.2]), SimpleNamespace(values=[0.3, 0.4])])
    with patch.object(data_loader.client.aio.models, "embed_content", AsyncMock(return_value=fake)):
        result = await data_loader.embed_texts(["a", "b"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_texts_raises_on_count_mismatch():
    fake = _fake_response([SimpleNamespace(values=[0.1, 0.2])])  # only 1 for 2 inputs
    with patch.object(data_loader.client.aio.models, "embed_content", AsyncMock(return_value=fake)):
        with pytest.raises(RuntimeError, match="mismatch"):
            await data_loader.embed_texts(["a", "b"])


async def test_embed_texts_raises_on_none_values():
    fake = _fake_response([SimpleNamespace(values=None), SimpleNamespace(values=[0.3, 0.4])])
    with patch.object(data_loader.client.aio.models, "embed_content", AsyncMock(return_value=fake)):
        with pytest.raises(RuntimeError, match="no values"):
            await data_loader.embed_texts(["a", "b"])
