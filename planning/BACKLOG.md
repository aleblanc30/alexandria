# Backlog

Nice-to-haves: wanted, but not competing for the next slot.

High-priority work lives in `TODO.md`. A brief line is enough; a what/why/shape
sketch is optional. Move an entry between the two when its priority changes rather
than listing it in both, and delete it when it ships.

## YouTube connector

### Transcript-based Phase-2 content extraction

**What:** Enrich YouTube video documents with their caption transcript (auto-generated
or manual) as embeddable full text, instead of only title + channel + description.

**Why deferred:** The initial YouTube connector ingests **metadata only** (title,
channel, description via the YouTube Data API v3). Transcripts add a new dependency
(`youtube-transcript-api`) and outbound calls to `youtube.com` per video, plus
handling for videos with captions disabled. Shipping metadata-only first keeps the
connector lean and lets the Data API path stabilise before layering content fetch on top.

**Rough shape when picked up:**
- Add `youtube-transcript-api` to `pyproject.toml` dependencies.
- New `pka/ingestion/youtube_transcript.py`: fetch transcript for a video ID,
  concatenate cues into text, handle `TranscriptsDisabled` / `NoTranscriptFound`
  gracefully (fall back to metadata-only, do not mark the doc unfetchable).
- Turn the metadata-only ingest into a true two-phase flow: Phase 2 fetches the
  transcript, builds `title + channel + description + transcript` via a helper
  (mirror `pka/ingestion/preprint_text.build_preprint_text`), and embeds via
  `ingest_text_block`. Offset chunk indices past Phase-1 chunks with
  `existing_chunk_count()` so both passes coexist in one document (see DESIGN §3).
- Prefer manually-authored captions over auto-generated; pick language by config
  (`ALEXANDRIA_YOUTUBE_TRANSCRIPT_LANGS`, default `["en"]`).
- Mock `youtube-transcript-api` in `tests/conftest.py`; add transcript cases to
  `tests/test_connector_youtube.py`.

### Watch Later ingestion (Data API can't reach it)

**What:** Include the user's **Watch Later** queue among the ingested saved videos.

**Why deferred / blocked:** The YouTube Data API v3 no longer exposes the special
`WL` playlist — `channels.list → contentDetails.relatedPlaylists.watchLater` was
removed years ago and no OAuth scope brings it back. So the current connector
(Liked videos + owned playlists) structurally cannot see Watch Later. Any fix
means reaching WL through a channel other than the official API.

**Options, ranked:**
1. **Convert WL to a normal playlist (durable, no code).** A regular playlist
   (e.g. "To Watch") *is* API-visible, so the connector already ingests it. Only
   helps going forward; no one-click WL→playlist copy exists in YouTube.
2. **yt-dlp with browser cookies (recommended bridge).** `yt-dlp` authenticates as
   the logged-in web client (InnerTube), which still serves WL:
   `yt-dlp --flat-playlist --dump-single-json --cookies-from-browser firefox
   "https://www.youtube.com/playlist?list=WL"`. Runs fully locally (fits
   local-first better than OAuth). Recommended shape: use yt-dlp **only to get WL
   video IDs**, then reuse the existing Data API `videos.list` hydration (public,
   per-ID, no OAuth) for title/channel/description/tags — confining the fragile
   part to one small loader. Add a `load_watch_later_ids_via_ytdlp()` behind a
   flag (`ALEXANDRIA_YOUTUBE_WATCH_LATER=1` + a cookies source), merge into
   `load_saved_videos()` as a playlist named `Watch Later`, add `yt-dlp` as an
   optional extra, and mock it in tests.
3. **Browser automation (last resort).** Scrape `youtube.com/playlist?list=WL`
   from a logged-in session. Strictly worse than yt-dlp; no upside.

**Caveats to document (in the DESIGN §2.1 cloud-exception note):** yt-dlp is not an
official API — InnerTube changes can break it, heavy scraping risks account
rate-limiting, and `--cookies-from-browser` reads the browser cookie store
(local but sensitive). Flat mode gives no reliable added-date ordering.

**Not a fix:** Google Takeout — Watch Later has historically been excluded from
Takeout's YouTube playlist CSVs; verify before relying on it.

## Retrieval enrichment

### Topical tags from the summarisation pass

*Prioritised in `TODO.md`; this section is its sketch.*

**What:** Generate a handful of topical tags per document (`overlay_tags`, machine
origin) from the same local chat call that already produces the `pass="summary"`
chunk, so a long article gains subject tags without a second inference pass.

**Why deferred:** The summarisation call exists but is default-off and narrow —
`_SUMMARY_FLAGS` in `pka/ingestion/core.py` gates it to Firefox/Reddit
(`bookmark_summary_enabled`) and Calibre (`book_summary_enabled`); Zotero, YouTube
and images never reach it. Tags therefore cannot simply ride along and call
themselves library-wide coverage. Two design questions also want settling before
code: whether machine tags reuse `TagOrigin.LLM` (already carrying cluster labels)
or get their own origin, and how they interact with the learned-tag path in
`pka/tag_training/`, which produces the same kind of artefact from user labels.

**Rough shape when picked up:**
- Extend the JSON contract in `_RULES_TAIL` (`pka/ingestion/summarize.py`) to
  `{"summary": ..., "tags": [...]}`. Both providers grammar-constrain to a valid
  JSON *object*, not to a schema (`format: "json"` for Ollama,
  `response_format: {"type": "json_object"}` for OpenAI-compatible), so parse
  tolerantly the way `_extract_summary` already handles key drift — accept a
  comma-joined string as well as a list.
- Change `_summarize_once` / `_summarize_recursive` to return a small result
  dataclass instead of `str | None`. Take tags from the **final reduce call**
  only, or union the map-pass tags and feed them into the reduce prompt for
  merging — per-chunk tags are per-section, not per-document.
- Persist via the existing write path: `slugify_tag()` then
  `insert_overlay_tags(con, [doc_id], tag, origin)` from
  `pka/clustering/cluster_tags.py`. Reusing `TagOrigin.LLM` needs no wiring; a new
  origin value means touching the filter branches in `pka/db/queries.py`
  (`_where_overlay_tag` callers) and the `overlay_origins` set in the tag-index
  query. Write at generation time — `attach_summary_chunk` replays a cached
  `documents.generated_summary` without re-inferring, so a cache hit must find
  the tags already present rather than regenerate them.
- **Coverage gap to decide on:** `summarize_text` short-circuits when the input is
  already within `summary_max_sentences` and spends no call at all — most
  bookmarks and Reddit posts land there. Piggybacking alone tags only long
  documents; tagging the rest means a dedicated cheap call for short text, which
  is a separate cost decision.
- Per DESIGN §1.1, gate tag generation behind its own named default-off setting
  rather than letting it escalate implicitly from `bookmark_summary_enabled`.
- Tests: extend `tests/test_summarize.py` (map/reduce tag merging, malformed
  `tags` payloads, cached-summary path) — the chat provider is already mocked in
  `conftest.py`.

### Chunking and map-reduce rework for billable chat providers

**What:** Re-derive the summarisation cost model in `pka/ingestion/summarize.py`
from the **active** chat provider, instead of the module-level constants tuned for
a free local model. Today `CHUNK_CHAR_LIMIT = 6000`, `MAX_CHUNKS_PER_PASS = 12`
and `MAX_REDUCE_DEPTH = 2` are fixed, so one long document costs up to ~13 chat
round-trips and drops its tail past ~72k characters — regardless of whether
`chat_provider` is local Ollama or a metered hosted backend
(`ollama_cloud` / `openrouter` / `ovh`).

**Why deferred:** The constants are correct for the backend they were written
against. `CHUNK_CHAR_LIMIT` is sized under a *small local* context window, and the
call count doesn't matter when calls are free — only wall-clock does. Both premises
invert on a billable API: hosted models carry far larger contexts, so most
documents that get map-reduced locally would fit in a **single** call, and the
map-reduce is then paying per request to work around a constraint that backend
does not have. Worse, every one of those ~13 calls re-sends the identical rules
preamble, and a Calibre bulk ingest multiplies the whole thing by the library.
Fixing this well means the provider layer exposing its limits, which it currently
does not — `ChatProvider` in `pka/providers/base.py` is only `resolve_model` +
`chat_json`, with no notion of context size, token cost, or budget.

**Rough shape when picked up:**
- Extend the `ChatProvider` protocol with the input budget it can accept (context
  window, or simply a usable character budget) and have `summarize.py` derive
  `CHUNK_CHAR_LIMIT` from the active provider rather than a constant. Large-context
  backends then collapse to the existing single-call path — which already exists,
  it just never triggers at 6000 chars.
- **Separate the two jobs the current bound is doing.** `MAX_CHUNKS_PER_PASS *
  CHUNK_CHAR_LIMIT` is simultaneously a context bound and a spend bound. A large
  context removes the first but not the second: a full-text book still must not be
  sent whole just because it fits. Keep an explicit per-document character ceiling
  independent of the provider's window.
- For books specifically, consider extract-then-summarise (front matter plus
  section openings) over full map-reduce — cheaper on any backend, and closer to
  how a topical summary is actually decided.
- **Spend visibility, not just spend bounds:** count calls and characters sent per
  ingestion run and log them, plus a per-run enrichment cap that stops summarising
  rather than quietly spending. A bulk ingest is where a per-document cost becomes
  a bill.
- Position the rules preamble as a stable prompt prefix so providers that price
  cached input lower can actually reuse it — it is byte-identical across every
  call today.
- Verify the `documents.generated_summary` cache genuinely short-circuits the
  purge-and-reingest path before any paid rollout; paying twice for the same
  document is the failure mode that matters most here.
- The local path must not regress: per DESIGN §1.1 hosted routing stays an explicit
  `*_PROVIDER` setting, and a fresh checkout keeps today's small-context behaviour.
- Tests: parameterise `tests/test_summarize.py` over a fake provider advertising a
  large budget and assert the single-call path, alongside the existing map-reduce
  cases.

## PDF ingestion

### OCR the documents that have no text layer

**What:** Give the `no_text_layer` set — scanned PDFs, recorded as such by
`extract_pdf_report` on the Calibre, Zotero-attachment and fetch routes — a way to become
searchable text: rasterise each page, run it through the existing OCR provider,
and feed the result into `ingest_text_block` like any other section.

**Why deferred:** The extraction half is the cheap half and it is already done;
the OCR half is a compute budget question, not a plumbing one. A 300-page scan
through a VLM on a 4 GB Pascal card is not a background task, so this needs a
page cap and a deliberate switch before it is worth having. Recording the
candidates first costs nothing and means the work queue already exists when the
budget question gets answered.

**Rough shape when picked up:**
- No new dependency for rasterisation: **pypdfium2** already ships as a
  pdfplumber dependency (BSD/Apache, unlike the AGPL PyMuPDF), and renders a
  page to a PIL image in two calls.
- No new provider either: `ocr_provider` (`vlm` | `easyocr`) and
  `image_extractor.ocr_image` already do exactly this job for images. The
  per-page call is the same call.
- Gate it: a named setting, default off, per DESIGN §1.1 — `vlm` routes to a
  possibly-hosted vision model, so the page images are document content leaving
  the machine. `easyocr` stays local and is the safe default backend for it.
- Cap it: a page budget per document, and select work with
  `fetch_status = 'no_text_layer'` rather than re-probing every PDF.
- Mind the skip filters: `calibre_sync._needs_fulltext` and
  `zotero_sync._load_zotero_items_for_fulltext` skip `no_text_layer` documents,
  so the full-text passes will not re-read a scan. An OCR pass has to write its
  chunks as `pass="fulltext"` (with page numbers) and move the status off
  `no_text_layer`, or those filters and this pass disagree about what is done.
- Chunk metadata already has somewhere to put the page number (`page_start` /
  `page_end`), and OCR is the one route that knows it exactly — one page in,
  one section out.
- Cross-check the numbers: VLMs invent plausible text on degraded regions, so
  anything numeric coming out of a scan should be treated as unverified.

## Archiving

### Wayback Machine submission

Submit live bookmark URLs to Save Page Now so each gains a durable public second
address. Requirements in [`WAYBACK.md`](WAYBACK.md). Blocked on its §1.1 premise —
whether entries will be cited by anyone but their owner, which decides whether
this sweeps the collection or a handful of fragile pages. It would be the first
outbound path that **publishes** (collection membership, irrevocably), so
`DESIGN.md` §1.1 gains a category row before it ships. Open choices in its §13.

## Configuration

### Editable settings panel (the write half)

Let `/settings` *set* the operational tier — provider and model per capability,
base URLs, the `DESIGN.md` §1.1 outbound flags — persisting to `.env` and
resetting the cached providers so a switch is live. The read-only report ships;
this is its phase 2, sketched in `SETTINGS_PANEL.md`.

### M-8: group `Settings` into submodels

Deferred after costing: `config.py`'s churn is growth (one setting per feature),
which nesting does not reduce. Plan and the reasoning in `M8_NESTED_SETTINGS.md`.
The piece still worth doing on its own is a shared **`RemoteBackend`** model for
`ollama_cloud` / `openrouter` / `ovh` / `scaleway` (16 duplicated declarations,
three parallel dicts in `settings_view`) — read the plan's instance-default trap
first.

## Tooling

### M-16: draw down the mypy override list

`pyproject.toml`'s `[[tool.mypy.overrides]]` freezes 20 modules with
`ignore_errors = true` (76 errors in 19 files with it removed, as of the
2026-09-09 audit). Not a discrete task: take a module off the list whenever an
item edits it anyway, as M-1 did for `pka.clustering.engine`. Open decision: a
target release (v0.1.0?) by which the list is empty.
