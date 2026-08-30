# TODO

High-priority work: one brief line per item.

Nice-to-haves live in `BACKLOG.md` instead. Neither file requires a detailed plan —
the split is priority, not how well an idea is worked out. Move an entry between the
two when its priority changes; do not duplicate it in both.

## Ingestion & deduplication

- [ ] **Deduplication of tags** — merge or collapse duplicate tag names (case, spacing, synonyms) so the tag index stays clean.
- [ ] **Deduplication of items** — detect and merge duplicate documents/items across sources (same URL, DOI, arXiv ID, etc.) instead of storing multiple records.
- [x] **Domain frequency report** — list all domain names from ingested items, sorted by frequency, to prioritize which domains deserve special fetch/handlers next.
- [ ] **Exempt preprint PDFs from the page cap** — `fetch_pdf_max_pages` caps every PDF route at 3 pages, so arXiv/bioRxiv now index only title + abstract + 3 pages.
- [x] **Make `_DomainRateLimiter` actually rate-limit** — `wait` now claims a slot under the lock instead of deriving a delay from a shared last-send time, so concurrent waiters on one domain are spaced rather than released together.

## Source connectors

- [x] **arXiv ingester** — Firefox fetch handler for arxiv.org (`pka/ingestion/arxiv.py`): export.arxiv.org API metadata + PDF; title and abstract on browse cards.
- [x] **bioRxiv ingester** — Firefox fetch handler for biorxiv.org (`pka/ingestion/biorxiv.py`): api.biorxiv.org DOI lookup + PDF; title and abstract on browse cards.

## Active learning

- [ ] **Show performance stats for active learning labels** — surface `train_stats` in the tag-training UI (accuracy, precision/recall on a hold-out slice of user labels, positive/negative counts, skipped embeddings) so the user can judge model quality before accepting.
- [ ] **Improve tagging interface to allow negative seed** — extend seed affordances (source tag and browse multi-select) so the user can mark documents as negative examples (`label=0`, `source=seed`) at session start, not only positives via the Yes/No queue or auto bootstrap.

## UI

- [ ] **Delete tags in the UI** — allow removing tags from the frontend (with appropriate API support and confirmation).

## CLI & assistant

- [x] **Build a CLI** — shipped as `pka/cli/` and the `alexandria` console script; `scripts/*.py` remain as thin shims.
- [ ] **Add chat/agent capability** — local-first conversational interface over the archive: retrieve relevant documents via semantic search, answer questions with Ollama, and cite source items (UI panel and/or CLI subcommand).

## MCP

- [ ] **MCP server for document search** — architect and ship an MCP server that lets a client search for documents in Alexandria.

## Installation

- [x] **Start menu shortcuts** — `INSTALL.md` §8 documents Start / Console / Stop shortcuts for the standalone install, backed by shipped `scripts/console.bat` (follows `server.log`) and `scripts/stop-server.bat`.
