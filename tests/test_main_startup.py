import pytest
from fastapi.testclient import TestClient

from fastapiproject import main


def test_app_starts_when_gemini_api_key_present(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    with TestClient(main.app):
        pass  # lifespan startup completed without raising


def test_app_fails_fast_when_gemini_api_key_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        with TestClient(main.app):
            pass
