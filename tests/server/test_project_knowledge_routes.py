"""Tests for the project knowledge-base routes.

A project's shelf has to hold documents, find passages in them, and — the part
worth guarding hardest — never hand a document to a different project.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _upload(client: TestClient, project: str, name: str, body: bytes, ctype: str = "text/plain"):
    return client.post(
        f"/v1/projects/{project}/documents",
        files={"file": (name, body, ctype)},
    )


CONTRATO = b"Clausula primeira.\n\nRescisao antecipada mediante aviso previo de 30 dias.\n"


# --- upload -----------------------------------------------------------------


def test_upload_indexes_text(client: TestClient) -> None:
    res = _upload(client, "Acme", "contrato.txt", CONTRATO)
    assert res.status_code == 201
    body = res.json()
    assert body["filename"] == "contrato.txt"
    assert body["project"] == "Acme"
    assert body["searchable"] is True
    assert body["chunks"] >= 1
    assert body["text_chars"] > 0


def test_upload_without_extractable_text_is_stored_but_not_searchable(
    client: TestClient,
) -> None:
    """An image belongs on the shelf; it just cannot be found by content."""
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    res = _upload(client, "Acme", "diagrama.png", png, "image/png")
    assert res.status_code == 201
    assert res.json()["searchable"] is False
    assert res.json()["chunks"] == 0


def test_upload_rejects_unsupported_type(client: TestClient) -> None:
    res = _upload(client, "Acme", "planilha.xlsx", b"PK\x03\x04junk", "application/zip")
    assert res.status_code >= 400
    assert "suportado" in res.text.lower()


def test_upload_rejects_empty_file(client: TestClient) -> None:
    assert _upload(client, "Acme", "vazio.txt", b"").status_code >= 400


# --- list -------------------------------------------------------------------


def test_list_is_scoped_to_the_project(client: TestClient) -> None:
    _upload(client, "Acme", "a.txt", CONTRATO)
    _upload(client, "Outro", "b.txt", b"documento alheio")

    data = client.get("/v1/projects/Acme/documents").json()
    assert [d["filename"] for d in data["data"]] == ["a.txt"]
    assert data["searchable_count"] == 1


def test_list_of_a_project_with_nothing(client: TestClient) -> None:
    data = client.get("/v1/projects/Vazio/documents").json()
    assert data["data"] == []


# --- search -----------------------------------------------------------------


def test_search_finds_the_passage_and_cites_the_file(client: TestClient) -> None:
    _upload(client, "Acme", "contrato.txt", CONTRATO)
    data = client.get("/v1/projects/Acme/knowledge/search", params={"q": "rescisao"}).json()
    assert len(data["data"]) == 1
    hit = data["data"][0]
    assert hit["filename"] == "contrato.txt"
    assert "rescisao" in hit["text"].lower()
    assert hit["score"] >= 1


def test_search_never_crosses_projects(client: TestClient) -> None:
    _upload(client, "Outro", "segredo.txt", b"informacao confidencial do outro projeto")
    data = client.get("/v1/projects/Acme/knowledge/search", params={"q": "confidencial"}).json()
    assert data["data"] == []


def test_search_with_an_empty_query(client: TestClient) -> None:
    _upload(client, "Acme", "a.txt", CONTRATO)
    data = client.get("/v1/projects/Acme/knowledge/search", params={"q": ""}).json()
    assert data["data"] == []


def test_search_limit_is_capped(client: TestClient) -> None:
    _upload(client, "Acme", "a.txt", CONTRATO)
    res = client.get("/v1/projects/Acme/knowledge/search", params={"q": "rescisao", "limit": 9999})
    assert res.status_code == 200


# --- download ---------------------------------------------------------------


def test_download_returns_the_bytes(client: TestClient) -> None:
    doc_id = _upload(client, "Acme", "contrato.txt", CONTRATO).json()["id"]
    res = client.get(f"/v1/projects/Acme/documents/{doc_id}/content")
    assert res.status_code == 200
    assert res.content == CONTRATO
    assert "attachment" in res.headers["content-disposition"]


def test_download_refuses_another_projects_document(client: TestClient) -> None:
    """The id alone must not be enough — the project has to match."""
    doc_id = _upload(client, "Acme", "contrato.txt", CONTRATO).json()["id"]
    assert client.get(f"/v1/projects/Outro/documents/{doc_id}/content").status_code == 404


def test_download_of_a_missing_document(client: TestClient) -> None:
    assert client.get("/v1/projects/Acme/documents/pdoc_nope/content").status_code == 404


# --- delete -----------------------------------------------------------------


def test_delete_removes_document_and_its_index(client: TestClient) -> None:
    doc_id = _upload(client, "Acme", "contrato.txt", CONTRATO).json()["id"]
    assert client.delete(f"/v1/projects/Acme/documents/{doc_id}").status_code == 204

    assert client.get("/v1/projects/Acme/documents").json()["data"] == []
    found = client.get("/v1/projects/Acme/knowledge/search", params={"q": "rescisao"}).json()
    assert found["data"] == []


def test_delete_refuses_another_projects_document(client: TestClient) -> None:
    doc_id = _upload(client, "Acme", "contrato.txt", CONTRATO).json()["id"]
    assert client.delete(f"/v1/projects/Outro/documents/{doc_id}").status_code == 404
    assert client.get("/v1/projects/Acme/documents").json()["data"]


def test_delete_of_a_missing_document(client: TestClient) -> None:
    assert client.delete("/v1/projects/Acme/documents/pdoc_nope").status_code == 404
