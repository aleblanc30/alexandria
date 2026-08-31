# TODO

High-priority work: one brief line per item.

Nice-to-haves live in `BACKLOG.md` instead. Neither file requires a detailed plan —
the split is priority, not how well an idea is worked out. Move an entry between the
two when its priority changes; do not duplicate it in both.

## Ingestion & deduplication

- [ ] **Deduplication of tags** — merge or collapse duplicate tag names (case, spacing, synonyms) so the tag index stays clean.
- [ ] **Deduplication of items** — detect and merge duplicate documents/items across sources (same URL, DOI, arXiv ID, etc.) instead of storing multiple records. `documents.doi` / `arxiv_id` / `isbn` are now joinable (indexed, canonical form) — see `DOCUMENT_METADATA_PLAN.md`.
- [x] **Persist structured document metadata** — every runner flattens authors/DOI/year into the embed blob, so the chunk text is the only copy; add `doi` / `arxiv_id` / `isbn` (all indexed), `year` and `authors_json` to `documents`, derive the DOI of a preprint from its arXiv ID, and split Zotero's `url_or_path` into `zotero_url` / `zotero_path`; plan in `DOCUMENT_METADATA_PLAN.md`.
- [ ] **Collapse the `documents` write-path signature** — `insert_document_if_new` and `upsert_document` (`pka/db/queries.py`) are raw SQL with hand-maintained column lists in three places each (INSERT, VALUES, `ON CONFLICT DO UPDATE`), now 16 parameters after `DOCUMENT_METADATA_PLAN.md`. Take a dataclass or `**fields` mapping, or move to a Core `insert()`, so a new column is one edit rather than six.
- [x] **Domain frequency report** — list all domain names from ingested items, sorted by frequency, to prioritize which domains deserve special fetch/handlers next.
- [ ] **Ingest Zotero PDF attachments** — `item.pdf_path` is recorded and never read, so Zotero indexes title + abstract only (DESIGN.md §3.2); needs a phase-2 pass mirroring `ingest_calibre_fulltext` over `extract_book_report`, offset by `existing_chunk_count()` — no new extraction machinery.
- [ ] **Exempt preprint PDFs from the page cap** — `fetch_pdf_max_pages` caps every PDF route at 3 pages, so arXiv/bioRxiv now index only title + abstract + 3 pages.
- [x] **Make `_DomainRateLimiter` actually rate-limit** — `wait` now claims a slot under the lock instead of deriving a delay from a shared last-send time, so concurrent waiters on one domain are spaced rather than released together.
- [ ] **Domain-aware fetch dispatch** — the fetch pool picks a URL off a flat queue and only then waits on the per-domain limiter, so a run of same-host URLs parks every worker on that host while other domains sit ready; plan in `FETCH_DISPATCH_PLAN.md`.

## Search / vectors

- [ ] **Evaluate a different text embedding model** — Chroma's `DefaultEmbeddingFunction` (`all-MiniLM-L6-v2`) is used as-is in `pka/storage/vector_store.py`; benchmark retrieval quality against something like `bge-small-en-v1.5` or `gte-small` before committing, since swapping models forces a full reindex via `rebuild_from_chunks` (Chroma collections are dimension-locked).

## Source connectors

- [x] **arXiv ingester** — Firefox fetch handler for arxiv.org (`pka/ingestion/arxiv.py`): export.arxiv.org API metadata + PDF; title and abstract on browse cards.
- [x] **bioRxiv ingester** — Firefox fetch handler for biorxiv.org (`pka/ingestion/biorxiv.py`): api.biorxiv.org DOI lookup + PDF; title and abstract on browse cards.
- [ ] **Youtube ingester** — Firefox fetch handler for youtube pages saved as bookmarks
- [ ] **reddit ingester** — Firefox fetch handler for reddit pages saved as bookmarks
- [ ] **amazon ingester** — extend current amazon ingester to other domains (.fr, .com, .in ...)


## Active learning

- [ ] **Show performance stats for active learning labels** — surface `train_stats` in the tag-training UI (accuracy, precision/recall on a hold-out slice of user labels, positive/negative counts, skipped embeddings) so the user can judge model quality before accepting.
- [ ] **Improve tagging interface to allow negative seed** — extend seed affordances (source tag and browse multi-select) so the user can mark documents as negative examples (`label=0`, `source=seed`) at session start, not only positives via the Yes/No queue or auto bootstrap.

## UI

- [ ] **Delete tags in the UI** — allow removing tags from the frontend (with appropriate API support and confirmation).

## CLI & assistant

- [x] **Build a CLI** — shipped as `pka/cli/` and the `alexandria` console script; `scripts/*.py` remain as thin shims.
- [ ] **Add chat/agent capability** — local-first conversational interface over the archive: retrieve relevant documents via semantic search, answer questions with Ollama, and cite source items (UI panel and/or CLI subcommand).

## MCP

- [ ] **MCP server for document search** — architect and ship an MCP server that lets a client search for documents in Alexandria; plan in `MCP_PLAN.md` (Zotero + Firefox first, read-only, HTTP client of the local API).

## Installation

- [x] **Start menu shortcuts** — `INSTALL.md` §8 documents Start / Console / Stop shortcuts for the standalone install, backed by shipped `scripts/console.bat` (follows `server.log`) and `scripts/stop-server.bat`.

## Ingestion

- [ ] Summarization calls fail silently.
- [ ] Add buttons to purge specific subsets of the data and to retrigger various points of the ingestion pipeline.
