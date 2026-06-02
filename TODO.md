# TODO

Backlog of planned work for Personal Knowledge Archive (PKA).

## Ingestion & deduplication

- [ ] **Deduplication of tags** — merge or collapse duplicate tag names (case, spacing, synonyms) so the tag index stays clean.
- [ ] **Deduplication of items** — detect and merge duplicate documents/items across sources (same URL, DOI, arXiv ID, etc.) instead of storing multiple records.
- [ ] **Domain frequency report** — list all domain names from ingested items, sorted by frequency, to prioritize which domains deserve special fetch/handlers next.

## Source connectors

- [ ] **arXiv ingester** — dedicated connector/handler for arxiv.org (metadata, PDF, stable IDs).
- [ ] **bioRxiv ingester** — dedicated connector/handler for biorxiv.org (preprint metadata and content).

## Active learning

- [ ] **Show performance stats for active learning labels** — surface `train_stats` in the tag-training UI (accuracy, precision/recall on a hold-out slice of user labels, positive/negative counts, skipped embeddings) so the user can judge model quality before accepting.
- [ ] **Improve tagging interface to allow negative seed** — extend seed affordances (source tag and browse multi-select) so the user can mark documents as negative examples (`label=0`, `source=seed`) at session start, not only positives via the Yes/No queue or auto bootstrap.

## UI

- [ ] **Delete tags in the UI** — allow removing tags from the frontend (with appropriate API support and confirmation).
