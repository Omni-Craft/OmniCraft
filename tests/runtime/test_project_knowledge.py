"""Tests for project knowledge extraction, chunking and scoring."""

from __future__ import annotations

import pytest

from omnicraft.runtime.project_knowledge import (
    CHUNK_MAX_CHARS,
    MAX_TEXT_CHARS,
    chunk_text,
    extract_text,
    query_tokens,
    score_chunk,
)

# --- extraction -------------------------------------------------------------


def test_extracts_plain_text_by_extension() -> None:
    assert extract_text(b"ola mundo", "notas.txt") == "ola mundo"
    assert extract_text(b"# titulo", "leia.md") == "# titulo"


def test_extracts_by_content_type_when_extension_is_unknown() -> None:
    assert extract_text(b"conteudo", "arquivo.bin", "text/plain") == "conteudo"


def test_binary_without_text_layer_yields_nothing() -> None:
    """An image is stored but not indexed — empty text, not an error."""
    assert extract_text(b"\x89PNG\r\n\x1a\n", "foto.png", "image/png") == ""


def test_broken_pdf_does_not_raise() -> None:
    """A corrupt upload must not fail the request; it just is not searchable."""
    assert extract_text(b"nao sou um pdf", "quebrado.pdf", "application/pdf") == ""


def test_text_is_capped() -> None:
    huge = ("a" * 1000).encode() * 500  # 500k chars
    assert len(extract_text(huge, "grande.txt")) == MAX_TEXT_CHARS


def test_decodes_invalid_utf8_without_raising() -> None:
    assert extract_text(b"ok \xff\xfe", "x.txt").startswith("ok ")


# --- chunking ---------------------------------------------------------------


def test_chunks_split_on_paragraphs() -> None:
    text = "Primeiro paragrafo.\n\nSegundo paragrafo.\n\nTerceiro."
    chunks = chunk_text(text)
    assert chunks
    assert "Primeiro paragrafo." in chunks[0]


def test_short_paragraphs_are_packed_together() -> None:
    text = "\n\n".join(["curto"] * 5)
    assert len(chunk_text(text)) == 1


def test_long_paragraph_is_hard_split() -> None:
    chunks = chunk_text("x" * (CHUNK_MAX_CHARS * 2 + 10))
    assert len(chunks) >= 2
    assert all(len(c) <= CHUNK_MAX_CHARS for c in chunks)


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("\n\n   \n\n") == []


# --- query tokens and scoring ----------------------------------------------


def test_query_tokens_drop_noise_and_dedupe() -> None:
    assert query_tokens("de a contrato CONTRATO rescisão") == ["contrato", "rescisão"]


def test_query_tokens_of_a_useless_query() -> None:
    assert query_tokens("a de o") == []


@pytest.mark.parametrize(
    ("text", "tokens", "expected"),
    [
        ("o contrato de rescisão", ["contrato", "rescisão"], 2),
        ("o contrato", ["contrato", "rescisão"], 1),
        ("nada aqui", ["contrato"], 0),
        ("CONTRATO em maiúsculas", ["contrato"], 1),
    ],
)
def test_score_counts_distinct_token_hits(text: str, tokens: list[str], expected: int) -> None:
    assert score_chunk(text, tokens) == expected
