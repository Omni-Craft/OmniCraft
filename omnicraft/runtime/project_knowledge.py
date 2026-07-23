"""Project knowledge base — turn uploaded documents into searchable text.

A project's documents are only useful if the agent can find the relevant piece,
so an upload goes through three steps here: extract the text, split it into
chunks, and match a query against those chunks.

Search is deliberately token matching with hit-count ranking rather than FTS5.
The local-memory tool already works that way, and the same query path then runs
unchanged on SQLite and PostgreSQL — a virtual table would have meant a second,
dialect-specific path for a knowledge base that is typically tens of documents.
"""

from __future__ import annotations

import io
import re

#: Text-ish extensions we can read straight off the bytes. Anything else needs
#: a real extractor (PDF) or is stored without being indexed.
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".html",
        ".xml",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".rb",
        ".sh",
        ".sql",
    }
)

#: Target size of one chunk, in characters. Big enough to carry an idea, small
#: enough that a hit points at a specific passage rather than a whole file.
CHUNK_TARGET_CHARS = 1200
#: Never emit a chunk longer than this, even when a paragraph runs long.
CHUNK_MAX_CHARS = 2000
#: Cap on the text we keep per document, so one enormous upload cannot fill the
#: table (and the model's context) on its own.
MAX_TEXT_CHARS = 400_000

#: Query tokens shorter than this carry no signal and are dropped.
_MIN_TOKEN = 3
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÿ_]+")


def extract_text(content: bytes, filename: str, content_type: str | None = None) -> str:
    """
    Pull searchable text out of an uploaded document.

    :param content: Raw file bytes.
    :param filename: Original filename — the extension decides the strategy.
    :param content_type: MIME type when the uploader supplied one.
    :returns: The extracted text, capped at :data:`MAX_TEXT_CHARS`. Empty when
        nothing readable came out (an image, or a scanned PDF with no text
        layer) — the document is still stored, just not findable by content.
    """
    lower = filename.lower()
    is_pdf = lower.endswith(".pdf") or (content_type or "").startswith("application/pdf")
    if is_pdf:
        return _extract_pdf(content)[:MAX_TEXT_CHARS]
    if any(lower.endswith(ext) for ext in _TEXT_EXTENSIONS) or (content_type or "").startswith(
        "text/"
    ):
        return content.decode("utf-8", "replace")[:MAX_TEXT_CHARS]
    return ""


def _extract_pdf(content: bytes) -> str:
    """Extract a PDF's text layer, or ``""`` when it has none / is unreadable."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def chunk_text(text: str) -> list[str]:
    """
    Split text into retrieval-sized chunks on paragraph boundaries.

    Paragraphs are packed together until the target size is reached, so a chunk
    stays a coherent passage instead of cutting mid-sentence. A single
    over-long paragraph is hard-split as a last resort.

    :param text: The document's extracted text.
    :returns: Non-empty chunks, in document order.
    """
    chunks: list[str] = []
    buffer = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > CHUNK_MAX_CHARS:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(para[:CHUNK_MAX_CHARS])
            para = para[CHUNK_MAX_CHARS:]
        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 2 <= CHUNK_TARGET_CHARS:
            buffer = f"{buffer}\n\n{para}"
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


def query_tokens(query: str) -> list[str]:
    """
    Reduce a query to the tokens worth matching on.

    :param query: The user's or agent's raw query.
    :returns: Lowercased tokens of at least :data:`_MIN_TOKEN` characters, in
        first-seen order and without repeats.
    """
    seen: list[str] = []
    for raw in _TOKEN_RE.findall(query.lower()):
        if len(raw) >= _MIN_TOKEN and raw not in seen:
            seen.append(raw)
    return seen


def score_chunk(text: str, tokens: list[str]) -> int:
    """
    Score one chunk against query tokens.

    :param text: The chunk's text.
    :param tokens: Tokens from :func:`query_tokens`.
    :returns: How many distinct tokens the chunk contains. ``0`` means no hit.
    """
    lowered = text.lower()
    return sum(1 for token in tokens if token in lowered)
