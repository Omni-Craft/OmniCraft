# Project knowledge base

A project can hold documents that every session filed under it can consult.
Attachments are per session, so a contract or spec that matters to the whole
project used to be re-uploaded into each conversation; now it goes on a shelf
once.

Projects themselves are implicit: one exists while at least one conversation
carries the `omni_project` label. There is no project table — everything here
is keyed by the project **name**.

## Using it

**Craftwork is not where this lives.** Open a project from the sidebar folder's
menu (**Abrir projeto**), or go to `/projects/<nome>`. The page shows the
knowledge base on top and the project's sessions below.

Upload a file, and the agent in any session of that project can reach it with
the `project_knowledge` tool:

> **Base do projeto:** cláusula de rescisão

The tool answers with the matching passages and the file each came from, so the
model can cite its source.

### Enabling the tool

`project_knowledge` is opt-in per agent spec. Add it to `tools.builtins`:

```yaml
tools:
  builtins:
    - name: project_knowledge
```

## What becomes searchable

| Uploaded | Stored | Searchable |
|----------|--------|-----------|
| `.txt`, `.md`, code, and other text-ish files | yes | yes |
| PDF with a text layer | yes | yes |
| Scanned PDF (no text layer), images | yes | **no** |

A document with no extractable text still belongs on the shelf — it is
downloadable, and the UI marks it **sem texto** rather than pretending the
agent can find it. `text_chars` records how much text was extracted; `0` is a
legitimate state, not a failure.

PDF extraction uses `pypdf`. A corrupt PDF does not fail the upload; it just
lands unsearchable.

> **Deployment gotcha.** Adding `pypdf` to `pyproject.toml` does not install it
> into the environment your server actually runs from. If PDFs upload with
> `text_chars: 0` while text files index fine, the dependency is missing there —
> install it into that environment and re-upload.

## How search works

Extracted text is split into chunks on paragraph boundaries (~1200 characters,
hard-capped at 2000), so a hit points at a readable passage instead of a cut
sentence. A query is reduced to tokens of 3+ characters, and chunks are ranked
by how many distinct tokens they contain.

**This is deliberately token matching, not FTS5.** The local-memory tool already
works this way, and the same query path then runs unchanged on SQLite and
PostgreSQL; a virtual table would have meant a second, dialect-specific path for
a base that is typically tens of documents. Semantic/embedding search is not
implemented.

## Isolation

The property worth stating plainly: **a search never crosses projects.**

- The `project_knowledge` schema accepts only `query` and `limit`. The agent
  cannot name the project — it comes from the session's label. Even if the model
  invents a `project` argument, the label decides.
- Download and delete refuse a document belonging to another project, even with
  its id. Knowing the id is not enough; the project in the path must match.

Both are pinned by tests.

## API

| Route | Does |
|-------|------|
| `GET /v1/projects/{project}/documents` | List, with a `searchable_count` |
| `POST /v1/projects/{project}/documents` | Upload (multipart `file`) |
| `GET /v1/projects/{project}/documents/{id}/content` | Download |
| `DELETE /v1/projects/{project}/documents/{id}` | Remove document, index and bytes |
| `GET /v1/projects/{project}/knowledge/search?q=` | Search passages |

Uploads reuse the session-attachment allowlist and limits, and are read under a
cap — an oversized file is refused without being buffered whole.

## Limitations

- **No semantic search.** Token matching finds "rescisão" in a document that
  says "rescisão", not one that says "término antecipado".
- **A session must be filed under a project** for the tool to work; otherwise it
  says so rather than returning an empty result.
- **No re-index command.** Changing the chunking rules only affects documents
  uploaded afterwards; re-upload to re-index.

## Where the code lives

| Piece | File |
|-------|------|
| Extraction, chunking, scoring | `omnicraft/runtime/project_knowledge.py` |
| Document + chunk store | `omnicraft/stores/project_document_store/` |
| Tables | `omnicraft/db/db_models.py` (`SqlProjectDocument`, `SqlProjectKnowledgeChunk`) |
| Routes | `omnicraft/server/routes/project_knowledge.py` |
| Tool | `omnicraft/tools/builtins/project_knowledge.py` |
| Project page | `web/src/pages/ProjectPage.tsx` |
| Project grouping (pre-existing) | `web/src/hooks/useConversations.ts`, `web/src/shell/Sidebar.tsx` |
