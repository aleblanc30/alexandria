# TODO

High-priority work, one brief line per item. Nice-to-haves live in `BACKLOG.md`;
move an entry between the two when its priority changes rather than listing it in
both. Delete an entry when it ships — the commit and `CHANGELOG.md` are the
record.

## Current priorities

In order; everything below this section is unordered.

None ranked at the moment.

## Maintainability & performance

`M-n` / `P-n` ids: M-1…M-13 and P-1…P-8 are `MAINTAINABILITY_PERFORMANCE_AUDIT.md`;
M-14 and P-9 onward are `MAINTAINABILITY_PERFORMANCE_AUDIT_2026-09-09.md`.

- [ ] **Shrink the import-layering baseline** — 9 `ignore_imports` in
  `pyproject.toml`'s contract: `classification → domains`, `enrichment_runs →
  ingestion.summarize`, `api → cli` (×3), `ingestion.{enrich,rechunk} → purge`,
  `purge → cli`, `providers.vlm_ocr → ingestion.image_extractor`. Each is a
  small move of the shared helper to the lower layer.

## Ingestion

- [ ] **OCR scanned PDFs** — Calibre books, Zotero attachments and fetched PDFs
  with no text layer are marked `no_text_layer` and get no body chunks; run
  their pages through the OCR provider instead. Sketch in `BACKLOG.md` →
  *OCR the documents that have no text layer*.
- [ ] **Deduplication of tags** — plan in `TAG_DEDUPLICATION.md`.
- [ ] **Deduplication of items** — plan in `ITEM_DEDUPLICATION.md` (link rather
  than merge).
- [ ] **Exempt preprint PDFs from the page cap** — `fetch_pdf_max_pages` caps
  every PDF route at 3 pages, so arXiv/bioRxiv index only title + abstract + 3
  pages.
- [ ] **Summarization calls fail silently.**
- [ ] **Extend the summarization call to include tag inference** — sketch in
  `BACKLOG.md` → *Topical tags from the summarisation pass*.
- [ ] **Re-fetch documents a handler now covers** — nothing re-fetches a
  `fetched` row, so a document fetched before its handler existed keeps the
  scrape. A one-off cleared 634 such rows on 2026-09-04; 23 `m.youtube.com/watch`
  rows predating the oEmbed handler remain. Build a scoped re-queue only if this
  recurs.
- [ ] **Re-queue worthless ingestions** — pages whose stored text is HTML, CSS or
  a paywall. `content_gate` now rejects these at fetch time; this is about rows
  fetched before it.
- [ ] **Stamp image enrichment provenance** — the last open part of the purge
  plan; see `PURGE_AND_PROVENANCE_PLAN.md`.
- [ ] **Train a classifier for the image gate instead of the VLM** — plan in
  `IMAGE_GATE_CLASSIFIER.md`.
- [ ] **Investigate whether backfill for Reddit is actually useful.**

## Clustering

- [ ] **Suggested merges in cluster diagnostics cannot be performed** — no
  backend or UI surface performs the merge.
- [ ] **Persist partial results from clustering runs.**

## Active learning

- [ ] **Show performance stats for active-learning labels** — surface
  `train_stats` in the tag-training UI (hold-out precision/recall, label counts,
  skipped embeddings) so model quality can be judged before accepting.
- [ ] **Allow negative seeds** — let the user mark documents as negatives
  (`label=0`, `source=seed`) at session start, from source tags and browse
  multi-select.

## UI

- [ ] **Make the top-unfetchable-domains list collapsible** (`DomainTopLists.vue`).
- [ ] **Learned tags are not displayed in the browse view.**
- [ ] **Delete tags in the UI** — with API support and confirmation.

## Discovery

- [ ] **Favourites** — mark a document as a favourite from the browse card and
  the detail panel, filter browse by it, and keep it as a signal the
  recommender below can weight. Nothing like it exists yet; reading lists
  (`reading_lists` / `reading_list_items`) are the closest structure.
- [ ] **Recommend items from a topic and the reading history** — given a topic
  (a query, a tag or a cluster), rank documents not yet read by similarity to
  what has been read and favourited on it, using `documents.doc_embedding`.
  Needs a reading history first: nothing records which documents were opened
  today, so decide what counts as read (opened in the detail panel, opened at
  the source, marked by hand) before building the ranking.

## CLI & assistant

- [ ] **Add chat/agent capability** — local-first conversational interface over
  the archive: semantic retrieval, answers via Ollama, cited source items (UI
  panel and/or CLI subcommand).

## MCP

- [ ] **MCP server for document search** — plan in `MCP_PLAN.md` (Zotero +
  Firefox first, read-only, HTTP client of the local API).
