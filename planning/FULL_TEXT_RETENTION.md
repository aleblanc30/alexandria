# Full-text retention — `document_texts`

**Status:** **shipped** — all three slices. The table and `text_store.py`, the
Firefox / Reddit / Calibre write sites with `blocks_json`, purge wiring,
`retain_document_text`, and the consumers: the `enrich` ladder, the `rechunk`
pass (`alexandria rechunk`, `POST /ingestion/rechunk`) and
`GET /documents/{id}/text`. One follow-up is deliberately left open — the size
measurement in §9 against the installed archive, which decides whether §8 needs
a cap. Supersedes the sketches it grew from:
`BACKLOG.md` → *Ingestion → Retain the raw extracted text alongside the chunks*,
and `PURGE_AND_PROVENANCE_PLAN.md` §5.2.2. Both stay as pointers; this file is
the worked-out version.

**One sentence:** keep the extracted body text of anything Alexandria *fetched
or extracted* in a compressed sidecar table, so summarising, chunking and
extraction can be redone later without going back to the network or to a
minutes-long PDF extraction.

---

## 1. What is thrown away today

Verified against the code, not assumed:

- **Fetch.** `FetchResult.text` (`ingestion/fetch_base.py:34`) is handed to
  `embed_fn` inside the worker loop (`fetcher.py:768-775`) and nowhere else.
  `_persist_fetch_result` (`fetcher.py:660-692`) writes `fetch_status`,
  `archive_url`, `title`, `card_summary` and the bibliographic fields — never
  the text. The `FetchResult` is garbage once the loop advances.
- **Firefox / Reddit runners.** `embed_fetched_text`
  (`runners/firefox.py:79-137`, its Reddit twin at `runners/reddit.py:140-198`)
  composes `fetched_embed_text(title, summary, text)` and passes it to
  `ingest_text_block`. The body is a local variable.
- **Calibre.** `ingest_calibre_fulltext` (`runners/calibre.py:210-290`) extracts
  sections, chunks each one, joins them into `full_text` for
  `attach_summary_chunk`, and drops both when the loop body ends.
- **The only surviving copy is `chunks.text`**, which is whitespace-normalised,
  cut into 5-sentence windows with 1 sentence of overlap, and has every window
  under `min_chunk_chars=80` discarded outright. `enrich.reassemble_chunk_text`
  exists solely to half-undo that, and its docstring is candid about the gaps it
  cannot close.

Two sources already keep verbatim text and are the precedent for the shape
proposed here: `reddit_items.body` and `images.ocr_text` / `images.description`.

## 2. What retention buys

1. **Re-summarising exactly.** `ingestion/enrich.py` currently summarises text
   reassembled from overlapping chunks *because the original is gone*
   (`PURGE_AND_PROVENANCE_PLAN.md` §5.2.1). This retires that compromise.
2. **Re-chunking without a re-fetch.** Changing `chunk_sentences`,
   `chunk_overlap`, `min_chunk_chars`, or the splitter itself is today as
   impossible as re-summarising was — and it is the first thing anyone wants
   after swapping the embedding model.
3. **Re-running extraction and gate changes over the existing corpus.** The
   `content_gate` interstitial work and the open *Re-fetch documents a handler
   now covers* TODO both had to re-fetch or hand-edit the live archive; with the
   raw text stored, "which stored bodies are consent walls?" is a local query.
4. **Auditing what the fetcher actually got** when a page ingests badly, instead
   of inferring it from the chunks it produced.
5. **A prerequisite** for a real FTS index over body text (§11) and for any
   reader / "show me the text" surface, including the MCP plan's
   `get_document_text`-shaped tool.

### Non-goals

- Not a byte archive of the HTML/PDF. That is Wayback's job (`archive_url`).
- Not a replacement for `chunks.text` as the plaintext grep surface — see §11.
- Not a backfill. Documents ingested before this ships have no stored text and
  get `NULL`; nothing reconstructs it (§4, *No backfill*).
- No new outbound calls, no new provider traffic. Purely local retention, so
  `DESIGN.md` §1.1's flag rules do not apply — the setting in §8 exists for disk,
  not for privacy.

## 3. Scope — which text gets stored

**Rule:** store a block of extracted text when it (a) has no other verbatim home
and (b) cost a network round trip or a slow local extraction to produce.
Everything cheap to re-read from its own source stays out; duplicating it would
be disk spent to avoid milliseconds.

| Text | Store? | Why |
|---|---|---|
| Firefox fetched body (HTML or remote PDF) | ✅ slice 1 | network; nothing else keeps it |
| Reddit link-post fetched body | ✅ slice 1 | same path, same fetcher |
| Calibre full-text sections | ✅ slice 2 | file is on disk but extraction is minutes/book |
| Zotero PDF attachment text | ✅ when that pass lands | same shape as Calibre; see `TODO.md` |
| Reddit inline selftext / comment | ❌ | already verbatim in `reddit_items.body` |
| Zotero abstract | ❌ | re-readable from the local Zotero DB in milliseconds |
| YouTube description | ❌ | metadata, re-readable from the API |
| Image OCR / description | ❌ | already verbatim in `images.ocr_text`, `images.description` |
| Generated summary | ❌ | already cached in `documents.generated_summary` |

Note in passing, not a task here: a Zotero abstract *is* truncated on the way
into `card_summary`, so the archive holds no full copy — but the Zotero database
does, and re-reading it is what a re-sync already does.

## 4. Schema

```python
document_texts = sa.Table(
    "document_texts",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "document_id", sa.Integer, sa.ForeignKey("documents.id"),
        nullable=False, unique=True,
    ),
    sa.Column("text", sa.LargeBinary, nullable=False),   # zlib(UTF-8)
    sa.Column("encoding", sa.Text, nullable=False, server_default="zlib"),
    sa.Column("char_count", sa.Integer),                 # uncompressed length
    sa.Column("content_hash", sa.Text),                  # sha256 of the plain text
    sa.Column("blocks_json", sa.Text),                   # section map, paginated sources
    sa.Column("extracted_at", sa.Integer),
)
```

- **Sidecar, not a `documents` column.** `documents` is scanned constantly —
  browse, tag filters, progress counts, the clustering read path — and hanging a
  multi-hundred-KB blob off every row would slow all of them for a value almost
  nothing reads. Same 1:1 shape as `reddit_items` and `images`, which exist for
  the same reason.
- **`text` is `zlib`-compressed.** Disk is the only real objection to the whole
  feature and compression answers most of it (§9). `encoding` is a one-column
  hedge so a later codec change is a migration, not an archaeology exercise.
- **`char_count`** so counts, dry-runs and the purge registry never decompress a
  row to say how big it is.
- **`content_hash`** so a re-fetch can tell "the page changed" from "same page,
  fetched again" — recorded in slice 1, acted on by nobody (§13).
- **`blocks_json`** — `[{index, title, page_start, page_end, offset, length}]`
  for paginated sources. Without it, re-chunking a book loses the
  `section_title` / `section_index` / `page_start` / `page_end` metadata that
  `ingest_text_block` writes today (`ingestion/core.py:70-80`), so the re-chunk
  would be a strict downgrade of the chunks it replaced. NULL for fetched HTML,
  which arrives as one undifferentiated blob from trafilatura.
- **No backfill.** Text for an already-ingested document is genuinely gone.
  Reassembling it from chunks and storing *that* would be the worst option
  available: it would look verbatim, and the audit use case (§2.4) would then be
  reading a reconstruction while believing it is the fetch. Rows appear only as
  documents are (re-)fetched or re-extracted.
- **Migration:** `meta.create_all` covers a brand-new table, so `init_db` needs
  no `ALTER` and stays idempotent. Add the table to
  `tests/test_schema_migration.py`'s coverage.

## 5. Write path

New module `pka/ingestion/text_store.py`:

```python
store_document_text(doc_id, text, *, blocks=None, dry_run=False) -> bool
load_document_text(doc_id) -> str | None
document_text_meta(doc_id) -> dict | None      # char_count, hash, extracted_at
```

- **Deliberately not inside `ingest_text_block`.** That function is called once
  per *block* (a Calibre book calls it per section) and once per *pass* — the
  generated summary goes through it too (`core.py:181`). A write there would
  either overwrite a body with a summary or need per-call policy arguments.
  Explicit calls from the runners keep the invariant plain: **one row is one
  document's body text.**
- **Call sites:**
  - `runners/firefox.py::embed_fetched_text` — store `text`, the body, **not**
    the `fetched_embed_text(title, summary, text)` composite. Title and card
    summary already live on `documents`; storing the composite would double them
    on every re-chunk.
  - `runners/reddit.py::embed_fetched_reddit_text` — same.
  - `runners/calibre.py::ingest_calibre_fulltext` — once per book, after the
    section loop, with `blocks`. It already builds `full_text` for
    `attach_summary_chunk`; that same string is what gets stored.
- **Order:** store the text *before* chunking. The network round trip is the
  expensive half; a Chroma or chunker failure should not also cost the text.
- **Idempotent upsert** on `document_id`, following the `reddit_items` pattern
  already in `db/queries.py:712-726` (select, then update or insert).
- **Never fatal.** Wrap the call in `try/except` and log, exactly as
  `attach_summary_chunk` does — retention must not cost a document its ordinary
  ingestion.
- **`dry_run` writes nothing**, like every other persist call in the runners.

## 6. Consumers

### 6.1 `enrich` — the compromise retires

`enrich._body_text` gains a ladder: stored text if present, else
`reassemble_chunk_text` as today. Two lines, and pre-retention documents keep
working. The module docstring currently *states* the text is not retained — it
must be corrected in the same commit, not left to mislead.

### 6.2 Re-chunk pass — new, and the one that pays

```
alexandria rechunk [--source] [--limit] [--dry-run]
POST /ingestion/rechunk[?source=]
```

For each document with a stored text: delete its body chunks and their vectors
(reuse `purge.body_chunk_predicate`, `purge._delete_chunks`,
`vector_store.purge_vectors` — do not re-implement), then replay
`ingest_text_block` from the stored text (per block when `blocks_json` is set,
so section and page metadata survive), then `refresh_document_embedding`.
Summary / metadata / external-synopsis chunks are left alone; they are not what
changed.

Shape it after `ingestion/enrich.py` and register it beside `enrich` rather than
growing a second parallel pipeline — §5.2's warning in the purge plan applies
here verbatim.

### 6.3 `GET /documents/{id}/text` — optional, slice 3

The audit surface from §2.4, and what an MCP text tool would call. Returns the
stored text (decompressed) plus `char_count` / `extracted_at`, 404 when there is
none. Read-only, no new machinery.

## 7. Purge and lifecycle integration

- **`purge_source._CHILD_TABLES` must include `document_texts`.** Ships with
  slice 1, non-optional: without it a source purge leaves rows keyed to deleted
  document ids. This is the single easiest thing to forget here.
- **`purge.TARGETS["fetched_text"]`** deletes the stored text along with the
  body chunks — it *is* the fetched text, and leaving it behind would let a
  "purge and re-fetch" cycle silently compare against a stale hash. Its `count`
  grows a `document_texts` key too: `purge.py`'s module docstring makes "every
  key `count` reports, `purge` reports with the same value" an invariant.
- **New target `document_texts`** (tier 3 — source-derived; `provenance=False`;
  retrigger: re-run the source's sync) so the disk can be reclaimed without
  dropping the chunks that are actually serving search.
- `vectors`, `summaries`, `machine_tags`, `image_text` are unaffected.

## 8. Settings

`retain_document_text: bool = True` in `pka/config.py`. Local-only, so
`DESIGN.md` §1.1's "named setting, default off" rule does not bind — that rule
governs outbound calls. Default **on**: the feature is worthless if it is off on
the archive you later want to re-chunk, and the cost is §9.

Three mechanical follow-ons, each enforced by an existing test:

- add it to the `_parse_bool` `@field_validator` list (`config.py:451-465`), or
  `ALEXANDRIA_RETAIN_DOCUMENT_TEXT=0` will not parse;
- add it to `settings_view.GROUPS["Chunking"]` — `tests/test_settings_view.py`
  asserts every `Settings` field appears in exactly one group;
- with it off, every consumer falls back to today's behaviour (reassembly in
  `enrich`, "nothing to do" in `rechunk`).

No size cap for now: `fetch_pdf_max_pages` and Calibre's `max_pages` already
bound the largest inputs. Add one only if measurement says otherwise.

## 9. Size budget

Measured read-only against this checkout's `data/archive.db` (251 documents,
21.4 MB):

- `chunks.text` totals **5.30 MB** (Calibre 5.14, Firefox 0.11, Reddit 0.05).
- A 4 000-chunk sample compresses to **29 %** with `zlib.compress(level=6)`.
- Chunk bytes exceed body bytes by the overlap factor — 5-sentence windows with
  1 sentence of overlap, so ≈ 1.25×.

So stored ≈ `0.29 / 1.25` ≈ **0.23 × current chunk bytes** — about 1.2 MB here,
under 6 % of the database file. Calibre dominates the corpus (97 % of chunk
bytes on this archive), which is precisely why it is slice 2: ship the cheap
sources, measure the real installed archive, then decide.

## 10. Slices

1. ~~**Storage + fetched sources.** Table, `text_store.py`, Firefox and Reddit
   write sites, purge wiring (§7), setting, docs.~~ **Shipped.** No behaviour
   change beyond the write; nothing reads the table yet.
2. ~~**Calibre.** Section join, `blocks_json`.~~ **Shipped** as
   `section_blocks(sections) -> (text, blocks)` in `text_store.py`, called from
   `ingest_calibre_fulltext` right after extraction — the same joined string now
   feeds both retention and `attach_summary_chunk`, so the two cannot drift.
   Empty sections are dropped and each one stripped, because `store_document_text`
   strips what it is given and an offset computed against an unstripped join
   would be off by the whitespace it removed. **Still open from this slice:** the
   size measurement against the installed archive that decides whether §8 needs a
   cap after all — Calibre is 97 % of the corpus's chunk bytes on the dev
   archive, so it is the only source that can make retention expensive.
3. ~~**Consumers.** `enrich` ladder, the `rechunk` pass,
   `GET /documents/{id}/text`.~~ **Shipped.** Two decisions worth recording,
   neither of them in the sketch above: the re-chunk writes its new chunks
   *before* deleting the superseded ones (an interruption then leaves duplicates
   a re-run cleans up, instead of a document with no body chunks at all), and it
   offsets new indices past the **highest index in use** rather than past the
   chunk count — a surviving summary chunk keeps the high index it was given
   when the body still sat underneath it, so counting would collide with it.

Each slice is independently shippable and independently useful; slice 3 is the
one that pays for the other two.

## 11. Deferred: an FTS index

`BACKLOG.md`'s warning stands and is worth restating: `chunks.text` must stay
plaintext, because despite the name, `mode="fulltext"` in
`api/routers/search.py:69` matches `documents.title` with `ILIKE` and nothing
else. Chunk text is therefore the only plaintext copy of the corpus, and
grepping it is the only way to answer "which document contains this exact
string". The compression argument in §4 applies to `document_texts` alone and
must not be reused to justify compressing `chunks.text`.

Once `document_texts` exists, an FTS5 index over it is the natural follow-on and
the thing that would change that calculus — a real keyword search, and
`mode="fulltext"` finally meaning what it says. Out of scope here; its own
backlog entry.

## 12. Tests

- `text_store` round-trips text through `zlib` including non-ASCII and a very
  large body; `char_count` and `content_hash` match the plain text.
- Storing twice for one document upserts rather than duplicating (the unique
  constraint holds).
- A fetched Firefox document ends ingestion with exactly one `document_texts`
  row whose text is the **body**, not the title/card-summary composite.
- The same for a Reddit link post; an inline self-post writes **no** row.
- `retain_document_text=False` writes nothing and leaves chunking unchanged.
- A `store_document_text` failure does not fail the document's ingestion
  (monkeypatch it to raise; assert chunks still land).
- `purge_source` on a source removes its `document_texts` rows; the
  `fetched_text` target's dry-run count equals what its purge reports.
- Slice 2: a Calibre book's `blocks_json` round-trips section titles and page
  ranges, and offsets slice the stored text back into the original sections.
- Slice 3: `enrich` prefers stored text over reassembly and still works without
  it; `rechunk` replaces body chunks while leaving summary chunks intact, and
  reproduces `page_start`/`page_end` from `blocks_json`.

## 13. Docs to update in the same commit

- **`docs/persisted-fields.md`** — a `document_texts` subsection under §2 *Side
  tables* (same treatment as `reddit_items`), and the per-source ✅/— story of
  who writes it. This is exactly the file's stated purpose: a reader must be able
  to trust the dash.
- **`docs/ingestion-flows.md`** — the Firefox, Reddit and (slice 2) Calibre
  graphs gain a `store_document_text` node ahead of the chunk tail, coloured as
  shared; plus a row in the *What is actually shared* matrix.
- **`DESIGN.md`** §3 (two-phase ingestion) gets a sentence on retention; §3.2 if
  and when the `enrich` ladder changes.
- **`BACKLOG.md`** and **`PURGE_AND_PROVENANCE_PLAN.md` §5.2.2** repointed at
  this file (done as part of writing it).
- `CHANGELOG.md` on ship.

## 14. Open questions

- **Does anything act on `content_hash`?** Proposal: slice 1 records it and
  nothing reads it. Auto-re-chunk on change is a policy decision that wants the
  re-fetch story settled first (the *Re-fetch documents a handler now covers*
  TODO is the same conversation).
- **Should `rechunk` re-run the summary?** Proposal: no. The summary is cached
  and unaffected by chunk size; re-running it would spend inference for nothing.
  `enrich` is the entry point when a summary genuinely needs redoing.
- **Blocks for fetched HTML?** Proposal: no. trafilatura returns one blob and
  inventing block boundaries would be fiction. Remote PDFs go through
  `extract_pdf_report` and *do* have real sections — worth revisiting in slice 2
  once the Calibre block format exists, since it is the same extractor.
- **Ordering against the Zotero PDF pass** (`TODO.md`): that pass should write
  `document_texts` from day one rather than be retrofitted. If it lands first,
  slice 2 grows a third call site.
