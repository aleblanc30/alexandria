# TODO

Backlog of planned work for Alexandria.

## Ingestion & deduplication

- [ ] **Deduplication of tags** — merge or collapse duplicate tag names (case, spacing, synonyms) so the tag index stays clean.
- [ ] **Deduplication of items** — detect and merge duplicate documents/items across sources (same URL, DOI, arXiv ID, etc.) instead of storing multiple records.
- [x] **Domain frequency report** — list all domain names from ingested items, sorted by frequency, to prioritize which domains deserve special fetch/handlers next.

## Source connectors

- [x] **arXiv ingester** — Firefox fetch handler for arxiv.org (`pka/ingestion/arxiv.py`): export.arxiv.org API metadata + PDF; title and abstract on browse cards.
- [x] **bioRxiv ingester** — Firefox fetch handler for biorxiv.org (`pka/ingestion/biorxiv.py`): api.biorxiv.org DOI lookup + PDF; title and abstract on browse cards.

## Active learning

- [ ] **Show performance stats for active learning labels** — surface `train_stats` in the tag-training UI (accuracy, precision/recall on a hold-out slice of user labels, positive/negative counts, skipped embeddings) so the user can judge model quality before accepting.
- [ ] **Improve tagging interface to allow negative seed** — extend seed affordances (source tag and browse multi-select) so the user can mark documents as negative examples (`label=0`, `source=seed`) at session start, not only positives via the Yes/No queue or auto bootstrap.

## UI

- [ ] **Delete tags in the UI** — allow removing tags from the frontend (with appropriate API support and confirmation).

## CLI & assistant

- [ ] **Build a CLI** — unified command-line entry point (e.g. `alexandria` or `python -m pka.cli`) for search, ingest, clustering, tag training, and stats, wrapping or replacing the current one-off scripts under `scripts/`.
- [ ] **Add chat/agent capability** — local-first conversational interface over the archive: retrieve relevant documents via semantic search, answer questions with Ollama, and cite source items (UI panel and/or CLI subcommand).
