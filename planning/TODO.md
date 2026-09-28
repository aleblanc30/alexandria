# TODO

High-priority work, one brief line per item. Nice-to-haves live in `BACKLOG.md`;
move an entry between the two when its priority changes rather than listing it in
both. Delete an entry when it ships — the commit and `CHANGELOG.md` are the
record.

## Current priorities

In order; everything below this section is unordered.

1. **Multilingual chunking** — *Search / vectors*.
2. **Multilingual embedding model** — *Search / vectors*; settles the model half
   of (1).
3. **Zotero PDF full text** — *Ingestion*.
4. **Full-text search** — *Search / vectors*.
5. **Zotero collection names as tags** — *Ingestion*.

## Maintainability & performance

`M-n` / `P-n` ids: M-1…M-13 and P-1…P-8 are `MAINTAINABILITY_PERFORMANCE_AUDIT.md`;
M-14 and P-9 onward are `MAINTAINABILITY_PERFORMANCE_AUDIT_2026-09-09.md`.

- [ ] **M-17: split `tag_training/lifecycle.py`** — move the scoring half
  (`apply_learned_tags_for_document`, run for every ingested document) into
  `tag_training/scoring.py`, leaving session lifecycle to the API. Sequence with
  M-6's `ingestion → tag_training` edge.
- [ ] **M-6: layering contract** — write the intended import layering down and
  enforce it with import-linter; break the `ingestion → tag_training` edge with a
  post-ingest hook registry. Comment the lazy imports that only defer heavy
  libraries.
- [ ] **M-18: hygiene** — delete `pka/api/schemas/common.py` (`Pagination`, zero
  importers) and triage the 14 `ARG001` unused arguments. Leave `PLR0913` and
  `TRY003` alone.

## Ingestion

- [ ] **Ingest Zotero PDF attachments** *(priority 3)* — `item.pdf_path` is
  recorded and never read, so Zotero indexes title + abstract only. A phase-2
  pass mirroring `ingest_calibre_fulltext`, offset by `existing_chunk_count()`,
  writing `document_texts` from day one.
- [ ] **Use Zotero collection names as tags** *(priority 5)* — plan in
  `COLLECTION_TAGS.md`, which extends it to Firefox bookmark folders.
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

## Search / vectors

- [ ] **Multilingual chunking** *(priority 1)* — `pka/ingestion/chunker.py` is
  English-only on both paths: `_SIMPLE_SENT_RE` needs an ASCII `[A-Z]` after
  `.!?`, the spaCy path loads `en_core_web_sm`, and scripts without `.!?` or
  spaces (CJK, Thai) yield no boundary at all. A single-sentence split makes
  `sentence_window_chunks` emit **the whole document as one chunk** — a silent
  retrieval failure. Needs script/language detection and a per-language splitter
  (or a character-window fallback); `trim_to_sentences` moves with it.
- [ ] **Swap the embedding model** *(priority 2)* — Chroma's default
  `all-MiniLM-L6-v2` is used as-is (`storage/vector_store.py`). Benchmark
  multilingual candidates (`bge-m3`, `multilingual-e5-small`,
  `paraphrase-multilingual-MiniLM-L12-v2`) before committing: collections are
  dimension-locked, so the swap is a full reindex — `alexandria rechunk`, then
  `rebuild_from_chunks`.
- [ ] **Full-text search** *(priority 4)* — `mode="fulltext"` is an unbounded
  `title ILIKE '%q%'` scan (`api/search_hits.py::fulltext_hits`), so there is no
  keyword index anywhere. Add an external-content FTS5 table over `title` +
  `card_summary` (and later `document_texts`), kept in sync by the
  `DocumentWrite` path, with a migration and backfill. Until it ships,
  `chunks.text` must stay plaintext: it is the only greppable copy.

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

## CLI & assistant

- [ ] **Add chat/agent capability** — local-first conversational interface over
  the archive: semantic retrieval, answers via Ollama, cited source items (UI
  panel and/or CLI subcommand).

## MCP

- [ ] **MCP server for document search** — plan in `MCP_PLAN.md` (Zotero +
  Firefox first, read-only, HTTP client of the local API).
