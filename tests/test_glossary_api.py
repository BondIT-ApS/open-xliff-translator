"""Tests for the glossary REST endpoints."""
import pytest
from fastapi.testclient import TestClient

import db
import glossary
from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client backed by an isolated database."""
    connection = db.connect(str(tmp_path / "api.db"))
    monkeypatch.setattr(db, "connection", connection)
    monkeypatch.setattr(db, "get_connection", lambda: connection)
    glossary.invalidate_cache()
    yield TestClient(app)
    connection.close()
    glossary.invalidate_cache()


class TestCreate:
    def test_create_returns_201(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"},
        )
        assert response.status_code == 201
        assert response.json()["target_term"] == "Bloker"

    def test_duplicate_returns_409(self, client):
        payload = {"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"}
        client.post("/api/glossary", json=payload)
        assert client.post("/api/glossary", json=payload).status_code == 409

    def test_blank_source_returns_422(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "  ", "target_term": "Bloker"},
        )
        assert response.status_code == 422

    def test_ampersand_in_source_returns_422(self, client):
        response = client.post(
            "/api/glossary",
            json={"target_lang": "da", "source_term": "R&D", "target_term": "X"},
        )
        assert response.status_code == 422


class TestList:
    def test_list_filters_by_language(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        client.post("/api/glossary", json={"target_lang": "de", "source_term": "Ban", "target_term": "Sperren"})
        assert len(client.get("/api/glossary?target_lang=da").json()) == 1

    def test_list_without_filter_returns_all(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        client.post("/api/glossary", json={"target_lang": "de", "source_term": "Ban", "target_term": "Sperren"})
        assert len(client.get("/api/glossary").json()) == 2


class TestUpdateDelete:
    def test_update_changes_target(self, client):
        term_id = client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Forbyde"}).json()["id"]
        response = client.put(f"/api/glossary/{term_id}", json={"target_term": "Bloker"})
        assert response.status_code == 200
        assert response.json()["target_term"] == "Bloker"

    def test_update_unknown_returns_404(self, client):
        assert client.put("/api/glossary/9999", json={"target_term": "X"}).status_code == 404

    def test_delete_returns_204(self, client):
        term_id = client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"}).json()["id"]
        assert client.delete(f"/api/glossary/{term_id}").status_code == 204
        assert client.get("/api/glossary").json() == []

    def test_delete_unknown_returns_404(self, client):
        assert client.delete("/api/glossary/9999").status_code == 404
