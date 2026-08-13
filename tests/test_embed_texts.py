from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fastapiproject import data_loader


def _fake_response(embeddings):
    return SimpleNamespace(embeddings=embeddings)


def test_embed_texts_returns_one_vector_per_input():
    fake = _fake_response([SimpleNamespace(values=[0.1, 0.2]), SimpleNamespace(values=[0.3, 0.4])])
    with patch.object(data_loader.client.models, "embed_content", return_value=fake):
        result = data_loader.embed_texts(["a", "b"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_texts_raises_on_count_mismatch():
    fake = _fake_response([SimpleNamespace(values=[0.1, 0.2])])  # only 1 for 2 inputs
    with patch.object(data_loader.client.models, "embed_content", return_value=fake):
        with pytest.raises(RuntimeError, match="mismatch"):
            data_loader.embed_texts(["a", "b"])


def test_embed_texts_raises_on_none_values():
    fake = _fake_response([SimpleNamespace(values=None), SimpleNamespace(values=[0.3, 0.4])])
    with patch.object(data_loader.client.models, "embed_content", return_value=fake):
        with pytest.raises(RuntimeError, match="no values"):
            data_loader.embed_texts(["a", "b"])
