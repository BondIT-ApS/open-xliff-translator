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


class TestCsvExport:
    def test_export_includes_header_and_rows(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        body = client.get("/api/glossary/export?target_lang=da").text
        assert "source_term,target_term,match_case,enabled,note" in body
        assert "Ban,Bloker" in body

    def test_export_is_a_csv_attachment(self, client):
        response = client.get("/api/glossary/export?target_lang=da")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]


class TestCsvImport:
    def _upload(self, client, text, mode="merge", lang="da"):
        return client.post(
            f"/api/glossary/import?target_lang={lang}&mode={mode}",
            files={"file": ("terms.csv", text.encode("utf-8"), "text/csv")},
        )

    def test_import_creates_terms(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\nTicket,Ticket,0,1,keep\n"
        response = self._upload(client, csv_text)
        assert response.status_code == 200
        assert response.json()["imported"] == 2
        assert len(client.get("/api/glossary?target_lang=da").json()) == 2

    def test_import_tolerates_utf8_bom(self, client):
        csv_text = "﻿source_term,target_term,match_case,enabled,note\nBlokeret,Blokeret,0,1,\n"
        assert self._upload(client, csv_text).json()["imported"] == 1

    def test_import_preserves_danish_characters(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nAccount,Brugerændring,0,1,\n"
        self._upload(client, csv_text)
        assert client.get("/api/glossary?target_lang=da").json()[0]["target_term"] == "Brugerændring"

    def test_invalid_row_is_skipped_not_fatal(self, client):
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n,Missing,0,1,\n"
        body = self._upload(client, csv_text).json()
        assert body["imported"] == 1
        assert body["skipped"] == 1
        assert len(body["errors"]) == 1

    def test_merge_keeps_existing_terms(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ticket", "target_term": "Ticket"})
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n"
        self._upload(client, csv_text, mode="merge")
        assert len(client.get("/api/glossary?target_lang=da").json()) == 2

    def test_replace_clears_existing_terms(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ticket", "target_term": "Ticket"})
        csv_text = "source_term,target_term,match_case,enabled,note\nBan,Bloker,0,1,\n"
        self._upload(client, csv_text, mode="replace")
        terms = client.get("/api/glossary?target_lang=da").json()
        assert len(terms) == 1
        assert terms[0]["source_term"] == "Ban"

    def test_round_trip_reproduces_the_table(self, client):
        client.post("/api/glossary", json={"target_lang": "da", "source_term": "Ban", "target_term": "Bloker"})
        exported = client.get("/api/glossary/export?target_lang=da").text
        self._upload(client, exported, mode="replace")
        terms = client.get("/api/glossary?target_lang=da").json()
        assert len(terms) == 1
        assert terms[0]["target_term"] == "Bloker"
