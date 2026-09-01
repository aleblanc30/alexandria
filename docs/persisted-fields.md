# What gets persisted, per document type

One row per storage location, one column per source. Answers "if I ingest a
Reddit comment, what actually ends up in the database, and where?" — the
complement to `docs/ingestion-flows.md`, which draws *how* it gets there.

> **Derived, and unverified by any test.** Like the flow graphs, this is a
> reading of `pka/` as of `trunk`; the code and `DESIGN.md` outrank it, so where
> they disagree this file is what's wrong. Sources of truth, in order:
> `pka/db/schema.py` (columns), `pka/ingestion/runners/<source>.py` and
> `pka/ingestion/image_pipeline.py` (what writes them),
> `pka/db/queries.py::DocumentWrite` (which columns an ingestion upsert may
> touch at all), `pka/ingestion/core.py` (the shared chunk tail).

Legend: ✅ always written · ⬛ written when the source has the value ·
🟪 flag-gated (default off unless noted) · — never written by this source.

---

## 1. `documents` — the unified row

Every source, images included, gets a row here. `(source, source_id)` is unique.

| Column | Zotero | Firefox | Calibre | Reddit | YouTube | Images |
|--------|:------:|:-------:|:-------:|:------:|:-------:|:------:|
| `id` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `source` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `source_id` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `title` | ✅ | ✅ ¹ | ✅ | ✅ | ✅ | ✅ |
| `url_or_path` | ⬛ | ✅ | ⬛ | ✅ | ✅ | ✅ |
| `archive_url` | — | 🟪 ² | — | 🟪 ² | — | — |
| `date_added` | ⬛ | ⬛ | ⬛ | ⬛ | ⬛ | ⬛ |
| `ingested_at` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `fetch_status` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `item_type` | ✅ | — | — | ✅ | — | — |
| `card_summary` | ⬛ | ⬛ | — | ⬛ | ⬛ | ⬛ |
| `note` | — | — | ⬛ | — | — | — |
| `doc_embedding` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `generated_summary` | — | 🟪 | 🟪 | 🟪 | — | — |
| `doi` | ⬛ | ⬛ ³ | — | ⬛ ³ | — | — |
| `arxiv_id` | ⬛ | ⬛ ³ | — | ⬛ ³ | — | — |
| `isbn` | — | — | ⬛ ⁴ | — | — | — |
| `year` | ⬛ | ⬛ ³ | ⬛ | ⬛ ³ | — | — |
| `authors_json` | ⬛ | ⬛ ³ | ⬛ | ⬛ ³ | — | — |
| `zotero_url` | ⬛ | — | — | — | — | — |
| `zotero_path` | ⬛ | — | — | — | — | — |
| `zotero_attachment_key` | ⬛ | — | — | — | — | — |

¹ A fetch handler may overwrite the bookmark title with the fetched page's own
(`FetchResult.title`).
² Wayback snapshot URL, written only when the fetch fell back to archive.org on
a 404 — `fetch_wayback_fallback`, which unlike the other flags defaults **on**.
³ Set only by the arXiv/bioRxiv fetch handlers during phase 2, never blanked.
⁴ Rejected rather than stored when the checksum fails — it is a join key.

### What each column means per source

| Column | Zotero | Firefox | Calibre | Reddit | YouTube | Images |
|--------|--------|---------|---------|--------|---------|--------|
| `source_id` | item key (8 char) | `moz_bookmarks.id` | Calibre book id | fullname `t3_…`/`t1_…` | video id | absolute file path |
| `title` | item title | bookmark title | book title | thread / comment title | video title | filename |
| `url_or_path` | `url` → PDF path fallback | bookmark URL | preferred format path | external target, else permalink | watch URL | file path |
| `date_added` | `dateAdded` | bookmark date (µs→s) | `timestamp` | item creation ¹ | earliest playlist add | EXIF `DateTimeOriginal` → mtime |
| `fetch_status` | `available` (PDF on disk) / `pending` | `pending` / `unfetchable` → `fetched` | `available` / `missing` → `no_text_layer` | `available` (self-post, comment), `pending` / `unfetchable` (link post) | `fetched` (no fetch phase) | `available` |
| `item_type` | Zotero `itemType` | — | — | `post` \| `comment` | — | — |
| `card_summary` | abstract, or the highlight for an annotation | `body_excerpt` of the fetched text | — | `body_excerpt` of the body | video description | vision description |
| `note` | — | — | over-long Calibre tags, newline-joined | — | — | — |

¹ Reddit exposes no "saved at" timestamp, so this is when the post or comment
was written, not when the user saved it.

---

## 2. Side tables

| Table | Zotero | Firefox | Calibre | Reddit | YouTube | Images |
|-------|:------:|:-------:|:-------:|:------:|:-------:|:------:|
| `source_tags` | Zotero tags | bookmark tags | Calibre tags ¹ | — ² | the video's own tags | — |
| `source_collections` | collection names | folder path | series name | `r/<subreddit>` | playlist titles | — |
| `overlay_tags` (`origin=inferred`) | `academic` + `paper`/`preprint` by item type | `academic` + `paper`/`preprint` by host | — | — | `video` | vision `image_type` ³ |
| `chunks` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `fetch_log` | — | one row per fetch attempt | — | link posts only | — | — |
| `reddit_items` | — | — | — | ✅ 1:1 | — | — |
| `images` | — | — | — | — | — | ✅ 1:1 |

¹ Only the short ones — a tag over `MAX_TAG_WORDS` words is diverted to
`documents.note` by `split_calibre_tags`.
² `RedditSaved.tags` exists but the runner never inserts it; the subreddit is
carried as a collection instead.
³ Written directly via `insert_overlay_tags`, not through the rule-based
`classify_document`.

`overlay_tags` also receives `manual` (user edits), `llm` / `cluster_l1` /
`cluster_l2` (clustering), and `learned` (tag training) rows — none of them
ingestion-time, all of them source-agnostic.

### `reddit_items` (Reddit only)

| Column | Value |
|--------|-------|
| `kind` | `post` \| `comment` |
| `subreddit` | display name, no `r/` prefix |
| `permalink` | canonical thread URL — kept because a link post's `url_or_path` is the *external* target |
| `external_url` | link-post target, else NULL |
| `body` | selftext / comment body, **verbatim** — neither the 280-char card excerpt nor the overlapped, whitespace-normalised chunks can reproduce it |

### `images` (Images only)

| Column | Written by | Notes |
|--------|-----------|-------|
| `document_id`, `path`, `filename`, `width`, `height`, `file_size`, `date_taken` | scan pass (`register_images`) | `path` is unique |
| `image_type` | the gate's label, else the vision classification | `unknown` until the embed pass runs |
| `ocr_text` | OCR pass | skipped under `--skip-ocr` |
| `description` | vision LLM | also mirrored to `documents.card_summary` |
| `books_json` | per-type cover prompt | JSON `[{title, authors, isbn}]`; cached so a later identifier lookup need not re-run the VLM |
| `clip_vector_id` | CLIP pass | id in the `alexandria_clip` Chroma collection |
| `indexed_at` | embed pass | NULL after the scan, set once extraction has run — this is what separates "registered" from "ingested" |
| `text_vector_id` | **nobody** | dead column; image text chunks are keyed by `document_id` in `chunks` like every other source |

Rejected images land in `image_rejections` (`path`, `reason`, `text_coverage`,
`image_type`, `rejected_at`) instead, and any rows an earlier pass wrote are
deleted — see `DESIGN.md` §3.1. The `image_tags` table is read by the API but
never written by ingestion; image tags live in `overlay_tags`.

---

## 3. `chunks` and the Chroma payload

Every source ends in the same tail: `ingest_text_block` chunks the text, upserts
to the `alexandria_chunks` Chroma collection, mirrors the row into SQLite
`chunks`, and refreshes `documents.doc_embedding`.

| `chunks` column | Filled for | From |
|-----------------|-----------|------|
| `document_id`, `chunk_index`, `text`, `token_count`, `vector_id` | every source | always |
| `chunk_pass` | Calibre, Images, plus any summary chunk | the Chroma metadata key `pass` |
| `resolved_by` | external-synopsis chunks only | which rung of the lookup ladder resolved the book |
| `source_ref` | external-synopsis chunks only | ISBN or Open Library work key |
| `ref_title` | external-synopsis chunks only | resolved book title (a shelf photo carries several) |
| `page_start`, `page_end` | Calibre full text only | real 1-based PDF pages; NULL for EPUB and every non-paginated source |

Chroma carries `document_id`, `source`, `chunk_index` and `title` on every chunk,
plus whatever the caller adds:

| Pass (`pass=`) | Written by | Extra Chroma metadata |
|----------------|-----------|-----------------------|
| *(unset)* | Zotero, Firefox, Reddit, YouTube, Images | `modality=image` for images |
| `metadata` | Calibre phase 1 | — |
| `fulltext` | Calibre phase 2 | `section_title`, `section_index`, `page_start`, `page_end` |
| `external_synopsis` 🟪 | Calibre, Images | `book_title`, `resolved_by`, `isbn`, `work_key` |
| `summary` 🟪 | Firefox, Reddit, Calibre | — |

Images additionally get a vector in the separate `alexandria_clip` collection,
whose metadata is `document_id`, `image_id`, `image_type`, `filename`, `path`,
`modality=clip` (`DESIGN.md` §3.3).

---

## 4. What text is actually embedded

The one genuinely source-specific thing after the connector: what gets handed to
`ingest_text_block`.

| Source | Embedded text | Fallback when it yields no chunks |
|--------|---------------|-----------------------------------|
| Zotero | title + `by <authors>` + abstract; for an `annotation`, the highlight alone | — |
| Firefox | title + card summary + fetched body (`fetched_embed_text`) | the composed blob |
| Calibre ph. 1 | title + `by <authors>` + description (HTML stripped) | title |
| Calibre ph. 2 | one block per extracted section — chapter or page group | — |
| Reddit | selftext / comment body | title |
| YouTube | title + channel + description + tags | title |
| Images | per-type content + vision description + OCR (`image_search_text`) | filename |

Plus, when the flag is on:

| Extra chunk | Sources | Flag | Default |
|-------------|---------|------|---------|
| Generated summary | Firefox, Reddit | `bookmark_summary_enabled` | off |
| Generated summary | Calibre (map-reduced over the full text) | `book_summary_enabled` | off |
| External book synopsis | Calibre, Images | `external_lookup_enabled` | off |

Summaries are cached in `documents.generated_summary`, so a purge-and-reingest
replays them without paying for inference twice. See `DESIGN.md` §3.2.
