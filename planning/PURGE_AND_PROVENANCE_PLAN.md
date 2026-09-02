# Selective purge, pipeline re-triggers, and enrichment provenance

Plan for the `planning/TODO.md` item under *Ingestion*:

> - [ ] Add buttons to purge specific subsets of the data and to retrigger
>   various points of the ingestion pipeline.

with the framing from the request that motivates it: **things should be
selectively purgeable when swapping a backend** (embedding, summarisation),
and — the open question — *maybe a database with lots of provenance would be
useful to keep the result of the work rather than delete it.*

## 1. The answer to the provenance question, up front

**Provenance stamping is a prerequisite for selective purge, not an
alternative to it.** This is the load-bearing conclusion of the plan.

"Purge the summaries made by the old model" is currently *unimplementable* —
not hard, unimplementable — because nothing in the schema records which model
made any artifact. `documents.generated_summary` is a bare `TEXT` column;
`images.description`, `images.ocr_text` and `images.books_json` likewise. The
best a button can do today is "purge **all** summaries", which throws away the
work done by the backend you are keeping along with the work done by the one
you replaced. So the stamp columns are not a nice-to-have layered on top of the
buttons — they are what makes the buttons precise enough to be worth pressing.

**Retention (keeping the old value instead of deleting it) is the genuinely
optional part, and the answer differs by artifact kind.** The split is not
about how expensive the artifact was to produce; it is about whether the old
value can still be *used* after the swap:

| Artifact | Retain the old one? | Why |
|---|---|---|
| Chunk vectors (Chroma) | **No** | Chroma collections are dimension-locked, so two embedding models cannot coexist in one collection. And the vectors are fully rebuildable from `chunks.text`, which is retained — `rebuild_from_chunks()` already does exactly this. Retaining costs a second collection and buys nothing. |
| `documents.doc_embedding` | **No** | Mean-pooled from chunk vectors; same dimension lock, same rebuild path. |
| `documents.generated_summary` | **Yes** | Small (a few hundred chars), expensive (an LLM call, possibly *paid* — see `BACKLOG.md` on billable chat providers), and genuinely comparable: reading the old and new summary side by side is how you judge whether the swap was an improvement. |
| `images.description` / `ocr_text` / `books_json` | **Yes** | Same shape as summaries, and far more expensive on this hardware — a VLM pass on a 4 GB Pascal card is slow enough that regenerating "just to see" is not casual. |
| Cluster labels | **Already retained** | `cluster_runs` + `accepted` already implements exactly this pattern (see §4). |

So: **stamp everything, retain text-valued model output, rebuild vectors.**
That is a much smaller change than "a provenance database", and it keeps the
part of the idea that pays.

## 2. What already exists

Worth knowing before designing anything, because one of the two backend swaps
named in the request is **already solved**:

| Capability | Where | Notes |
|---|---|---|
| Purge an entire source | `pka/cli/purge_source.py`, `POST /ingestion/sources/{source}/purge`, Purge button in `IngestionSourcePanel.vue` | All-or-nothing: documents, chunks, vectors, tags, fetch log |
| Purge cluster runs | `pka/cli/purge_cluster_runs.py`, `alexandria purge-cluster-runs` | Per-run or `--all`, with `--dry-run` and an `--force` guard on the accepted run |
| **Rebuild all vectors** | `POST /ingestion/rebuild-vectors`, `vector_store.rebuild_from_chunks()` | Drops the collection and re-embeds from `chunks.text`. **This is the embedding-swap button, and it already ships.** |
| Clear image gate rejections | `clear_image_rejections()`, `alexandria images --reset-rejections` | Keyed by path, else rejected images stay skipped forever |
| Re-queue unfetchable URLs | `reset_unfetchable_for_fetch()` | Runs automatically at fetch start, once the retry cooldown elapses |

So the embedding-swap workflow is: change the model, hit rebuild-vectors, done.
**The summarisation-swap workflow has nothing**, and that gap is the real
content of this TODO item.

### 2.1 Two findings that motivate the work

**(a) Re-summarising currently requires nuking the source.** `attach_summary_chunk`
(`pka/ingestion/core.py:164`) short-circuits on the `documents.generated_summary`
cache — by design, so a re-ingest doesn't pay for inference twice. The only way
to clear that cache today is to delete the document row, i.e. `purge_source`.
That means **swapping the summarisation backend and re-summarising costs a full
re-fetch of every bookmark URL over the network**, plus re-reading every Zotero
/ Calibre database. The expensive thing (fetched text) gets destroyed to
invalidate the cheap thing (a cached summary string).

**(b) `purge_source` destroys user-authored data.** `_CHILD_TABLES`
(`pka/cli/purge_source.py:36`) includes `overlay_tags` unfiltered, so a purge
deletes `origin=manual` tags — hand-applied by the user — and `origin=learned`
tags, the output of an active-learning session, alongside the machine-derived
`origin=llm` / `cluster_*` ones. `reading_list_items` goes too. None of that is
recoverable by re-ingesting, because the source system never had it. A user who
purges Firefox to re-fetch some broken bookmarks silently loses every manual
tag on every bookmark.

Finding (b) is a **pre-existing bug**, not something this feature introduces,
but it is squarely in scope: the whole point here is purging *precisely*, and
the coarsest existing purge is imprecise in a way that costs irreplaceable
work. Fix it as part of this (§5.1).

## 3. The data taxonomy the buttons should follow

Every purge button is a choice about which of three tiers to touch. Getting
this taxonomy explicit is most of the design:

**Tier 1 — Irreplaceable (never purged by a backend swap).**
`overlay_tags` where `origin IN (manual, learned)`, `reading_lists` /
`reading_list_items`, `tag_training_sessions` / `tag_training_labels`. Authored
by the user or distilled from their labelling. No re-run reproduces these. A
button that deletes these must say so in as many words and be separate from
everything else.

**Tier 2 — Model-derived (the actual target of this feature).**
Chunk vectors, `doc_embedding`, `generated_summary` and its `pass='summary'`
chunk, `images.description` / `ocr_text` / `books_json`, CLIP vectors,
`overlay_tags` where `origin IN (llm, cluster_l1, cluster_l2, inferred)`,
cluster labels. Reproducible by re-running inference, at a cost in time and
possibly money. **These are what a backend swap invalidates**, and what wants
provenance.

**Tier 3 — Source-derived (cheap-ish, reproducible without inference).**
`documents` rows and their metadata, `source_tags`, `source_collections`,
`reddit_items`, fetched body text in `chunks` where `pass='fulltext'`,
`fetch_log`. Reproducible by re-running the connector — but "cheap" is doing
work in that sentence: re-fetching thousands of bookmark URLs is slow, rate-
limited, and pesters other people's servers. Prefer not to.

The existing `purge_source` collapses all three tiers into one button. The new
buttons should each name a tier and a scope.

## 4. Precedent to copy: `cluster_runs`

The repo already contains a worked example of "keep the result of the work
rather than delete it": `cluster_runs` (`pka/db/schema.py:160`) records
`timestamp`, `algorithm`, `parameters` (JSON), `status`, `notes` and an
`accepted` flag; `clusters.run_id` and `cluster_assignments.run_id` point back
at it; `purge_cluster_runs` deletes a run by id and refuses to drop the
accepted one without `--force`.

That is exactly the shape the request is reaching for, already in the codebase
and already understood. **The provenance design below is a generalisation of
`cluster_runs`, deliberately — not a new invention.** Same columns, same
supersede-rather-than-delete instinct, same per-run purge.

## 5. Phase 1 — Selective purge and re-trigger on today's schema

Ships value without any migration. Everything here is implementable now.

### 5.1 Fix the Tier-1 leak first

In `pka/cli/purge_source.py`, stop deleting user-authored rows in the
source purge:

- `overlay_tags` — filter to machine origins
  (`origin NOT IN ('manual', 'learned')`) instead of deleting by `document_id`
  alone.
- `reading_list_items` — keep. A reading list pointing at a re-ingestible
  document should survive; the alternative is silently emptying the user's
  lists. (This does leave dangling `document_id`s between purge and re-ingest —
  acceptable, and the list view already has to tolerate a missing document.)

Add `--include-user-data` to the CLI and a matching explicit flag on the API
for the rare "actually wipe it all" case, defaulting off. This is a behaviour
change to an existing command, so it needs a line in the purge endpoint's
response and a note in the UI confirm dialog.

### 5.2 A purge-target registry

Rather than a bespoke endpoint per button, define the targets in one place —
`pka/purge.py` (new) — as a small table of named operations:

```python
@dataclass(frozen=True)
class PurgeTarget:
    key: str                       # "summaries", "vectors", "image_text", …
    label: str                     # UI label
    tier: int                      # 2 = model-derived, 3 = source-derived, 1 = user
    count: Callable[[str | None], int]      # dry-run count, optional source filter
    purge: Callable[[str | None], dict]     # returns per-table counts
    retrigger: str | None          # endpoint/CLI that regenerates it, if any
```

One registry means the API, the CLI and the UI all enumerate the same set, and
a new target is one entry rather than four edits. It also gives the UI its
dry-run counts for free — every button can say "this will clear 1,432
summaries" before you press it, the way `purge_source --dry-run` already does.

Initial targets:

| Key | Clears | Re-trigger |
|---|---|---|
| `summaries` | `documents.generated_summary = NULL` + `chunks WHERE chunk_pass='summary'` (and their vectors) | **`POST /ingestion/enrich?kind=summary`** — see §5.2.1 |
| `vectors` | Chroma chunk collection + `chunks.vector_id = NULL` + `doc_embedding = NULL` | `POST /ingestion/rebuild-vectors` (exists) |
| `image_text` | `images.description`, `ocr_text`, `books_json`, **and `indexed_at = NULL`** | re-run image sync — see §5.2.1 |
| `clip_vectors` | `alexandria_clip` collection + `images.clip_vector_id` | re-run image sync with `clip_enabled` |
| `machine_tags` | `overlay_tags WHERE origin IN (llm, cluster_l1, cluster_l2, inferred)` | re-run clustering / tag passes |
| `fetched_text` | `chunks WHERE chunk_pass='fulltext'` + `fetch_status → pending` | re-run source ingest (re-fetches) |
| `fetch_failures` | `fetch_status='unfetchable' → 'pending'` + `fetch_log` rows | re-run source ingest |
| `cluster_runs` | delegates to existing `purge_cluster_runs` | re-run clustering |

Each takes an optional `source` filter, so "purge Firefox summaries" and
"purge all summaries" are the same code path.

**Re-trigger is mostly just purge + the existing sync entry point.** The
pipeline already skips work that exists (`skip_existing`,
`document_ids_with_chunks`, `existing_chunk_count`, the `generated_summary`
cache), so clearing the thing that causes the skip *is* the re-trigger. That
holds for `fetched_text`, `fetch_failures`, `machine_tags` and `vectors`, and
it is why the feature is much smaller than it sounds — resist adding a
parallel set of "regenerate X" pipelines.

It does **not** hold for the two Tier-2 targets this feature exists for. See
§5.2.1 before implementing either.

### 5.2.1 The skip gates are keyed on the wrong thing

Verified against the code, not assumed. Both expensive-model-output targets
purge cleanly and then never regenerate, because the gate that decides whether
to skip a document is keyed on a *different* artifact than the one purged:

- **`summaries`** — `_ingest_fetched_document` (`runners/firefox.py:99-103`)
  returns early when `doc_id in document_ids_with_chunks(...)`, i.e. "has any
  chunk at all". `attach_summary_chunk` is called at line 122, *after* that
  gate. Purging `generated_summary` and the `pass='summary'` chunk leaves the
  document's `pass='fulltext'` chunks in place, so the next sync skips it
  entirely and the summary never comes back. The `generated_summary` cache
  miss described in §2.1(a) is real but unreachable — control flow never gets
  there.
- **`image_text`** — same shape. `_image_already_embedded`
  (`image_pipeline.py:149`) gates on `images.indexed_at IS NOT NULL`, which a
  `description` / `ocr_text` / `books_json` purge does not touch.

Without a fix, both buttons are traps: they delete expensive artifacts and
offer no way back short of `skip_existing=False` or a full source purge —
precisely the workflow this TODO exists to eliminate.

The two cases need different fixes, and only one needs new machinery:

**`image_text` — no new pipeline.** The image file is still on disk, so
regeneration costs VLM time and nothing else. Have the purge also set
`images.indexed_at = NULL` (noted in the table above). The existing image sync
then picks the image up on its own and "purge is the trigger" genuinely holds.

**`summaries` — needs a pass, and a decision about text.** There is no
sentinel to clear: the skip gate is "has chunks", and the whole point is *not*
to delete the fulltext chunks. So this target needs a real entry point:

```
POST /ingestion/enrich?kind=summary[&source=]
alexandria enrich summary [--source] [--dry-run]
```

iterating documents where `generated_summary IS NULL` and chunks exist, calling
`attach_summary_chunk` directly. Parameterise it by `kind` from the start
(`summary` today, `image_text` if the `indexed_at` trick ever proves
insufficient) so this stays *one* endpoint rather than the parallel set warned
against above.

**The wrinkle: the fetched body text is not retained anywhere verbatim.**
After ingestion it survives only as `chunks.text` — overlapped and
whitespace-normalised. So the enrich pass must either re-fetch every URL
(defeats the purpose entirely: the expensive network work is destroyed to
redo the cheap inference) or summarise text reassembled from the fulltext
chunks.

**Decision: summarise reassembled chunks.** The overlap is deterministic and
strippable, and a summariser is robust to imperfect joins — this is not
extraction, it is gisting. Re-fetching is the option that makes the whole
feature pointless, so the lossiness is worth it. Calibre and Zotero are
unaffected either way, since the book/PDF is still on disk and re-extractable.

This decision has a shelf life: see §5.2.2.

### 5.2.2 Retaining raw text (future, and it retires the wrinkle)

The reassembly compromise above exists only because the raw extracted text is
thrown away once chunked. **The intended direction is to keep it** — a little
disk in exchange for never facing this trade-off again.

Rough shape when picked up:

- A separate `document_texts` table (`document_id`, `text`, `extracted_at`,
  maybe `content_hash`), **not** a `documents.raw_text` column. `documents` is
  scanned constantly by browse, tags, progress counts and the clustering read
  path; hanging a multi-hundred-KB blob off every row would slow all of them
  for a value almost nothing reads. A sidecar table keeps the hot table narrow
  and matches the shape `reddit_items` / `images` already use.
- Written where the text is first available — the fetcher and the extractors,
  next to where chunking happens today.
- Stored `zlib`-compressed. Space is the only real objection and compression
  answers most of it; the table is a near-duplicate kept for regeneration, and
  `chunks.text` stays plaintext for ad-hoc searching. See the `BACKLOG.md`
  entry for why that argument does not extend to `chunks.text` itself.

What it unlocks, beyond retiring §5.2.1's compromise: re-chunking with a
different chunk size or splitter without re-fetching (today that is as
impossible as re-summarising was), re-running extraction-quality changes over
the existing corpus, and a genuine "what did the fetcher actually get" audit
when a page ingests badly.

Tracked in `BACKLOG.md`; not a prerequisite for Phase 1 — the enrich pass ships
against reassembled chunks and simply gets more accurate when this lands.

### 5.3 Surface

- **API**: `GET /ingestion/purge-targets` (registry + dry-run counts),
  `POST /ingestion/purge/{key}?source=&confirm=` beside the existing
  `sources/{source}/purge`. Same `sp.is_running()` guard — a purge must not
  race a live worker.
- **CLI**: `alexandria purge <target> [--source] [--dry-run]`, mirroring
  `purge-source`'s flags. Keep `purge-source` as-is; it is the Tier-3 nuke.
- **Frontend**: a *Maintenance* panel on `/ingestion` (below the domain
  tables from `archive/DOMAIN_TOP_LISTS_PLAN.md`), one row per target: label, current
  count, Purge button, and a Re-trigger button where `retrigger` is set.
  Destructive, so each needs a confirm step showing the dry-run count; reuse
  the pattern already in `IngestionSourcePanel.vue:270`.

## 6. Phase 2 — Provenance stamping

The additive half of the answer to the request. Two new tables plus stamp
columns; no data migration, `init_db` stays idempotent.

### 6.1 `enrichment_runs` — generalised `cluster_runs`

```python
enrichment_runs = sa.Table(
    "enrichment_runs", meta,
    sa.Column("run_id", sa.Integer, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),      # summary|image_description|ocr|book_extract|embedding
    sa.Column("provider", sa.Text),                  # ollama|openrouter|ovh|scaleway|vlm|easyocr|clip
    sa.Column("model", sa.Text),                     # resolved model name, not the config default
    sa.Column("parameters", sa.Text),                # JSON: temperature, chunk limits, prompt version
    sa.Column("started_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.Column("status", sa.Text, default="running"), # running|finished|failed|cancelled
    sa.Column("notes", sa.Text),
)
```

`model` must be the **resolved** name (`provider.resolve_model()`), not the
config value — `chat_model` defaults to `""` meaning "auto-detect the first
non-embedding model from `/api/tags`", so storing the config value would record
nothing useful on the most common local setup.

### 6.2 Stamp columns

Additive, nullable, on the tables that already hold the artifact:

- `documents.summary_run_id` → `enrichment_runs.run_id`
- `images.description_run_id`, `images.ocr_run_id`, `images.books_run_id`

Nullable is doing real work here: every artifact produced *before* this ships
has genuinely unknown provenance, and `NULL` says so honestly. Do not
backfill with a guess — "made by whatever is in config right now" is a lie
that a purge would then act on.

### 6.3 What stamping unlocks

Purge targets gain a `run_id` / `provider` / `model` filter, so the buttons go
from "purge all summaries" to:

- purge summaries made by a specific run;
- purge summaries made by `provider=ollama, model=qwen2.5:3b`;
- purge summaries with **unknown** provenance (`run_id IS NULL`) — the
  pre-provenance backlog, cleared once and never again;
- keep everything made by the backend you are still using.

Plus a *Runs* view: what ran, when, with which model, over how many documents.
That is the "lots of provenance info" from the request, and it is genuinely
useful beyond purging — it is also the spend-visibility surface that
`BACKLOG.md` asks for under *Chunking and map-reduce rework for billable chat
providers* ("count calls and characters sent per ingestion run and log them").
**Design `parameters` with that second consumer in mind** — add
`calls`/`chars_sent` counters to the run row — because it makes the same table
answer "what did this cost" as well as "what made this".

## 7. Phase 3 — Retention (optional, decide after Phase 2 ships)

Only for text-valued model output, per §1. Shape if picked up:

```python
document_summaries = sa.Table(
    "document_summaries", meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("run_id", sa.Integer, sa.ForeignKey("enrichment_runs.run_id")),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.Column("superseded_at", sa.Integer),          # NULL = current
    sa.Index("ix_document_summaries_doc", "document_id"),
)
```

`documents.generated_summary` stays as a denormalised pointer to the current
row, so no read path changes — the same trick `chunks.chunk_pass` already uses
to mirror Chroma metadata into SQLite for the API's benefit.

Then "purge" becomes "supersede": stamp `superseded_at`, leave the text. A
`--hard` flag actually deletes. The UI can show old vs. new summary for one
document, which is the concrete thing that makes a backend swap judgeable
rather than a leap of faith.

**Do not build this speculatively.** It is only worth it if, after Phase 2,
you find yourself wanting the old value back. Phase 2's stamping is what makes
that question answerable; Phase 3 is the answer if it turns out to be yes.
The `image_enrichments` equivalent follows the same shape if it earns its way in.

## 8. Tests

**Phase 1** (`tests/test_purge.py`, new; `tests/test_purge_source.py` exists
for the source path):
- each registry target's dry-run count matches what its purge then deletes;
- `source=` filter scopes correctly and leaves other sources untouched;
- **`purge_source` preserves `origin=manual` / `origin=learned` overlay tags**
  and reading-list items (the §5.1 regression — this is the one that protects
  irreplaceable data, write it first);
- `--include-user-data` still removes them when explicitly asked;
- purging `summaries` clears the `pass='summary'` chunk *and* its vector, and
  leaves `pass='fulltext'` chunks intact;
- purge is refused while a sync is running (409);
- **the §5.2.1 round trip, per target: purge → re-trigger → the artifact is
  actually back.** This is the test that would have caught the wrong-skip-gate
  bug, and it is worth writing before the purge targets themselves. For
  `summaries`, assert the enrich pass regenerates a summary for a document
  whose fulltext chunks were left in place; for `image_text`, assert the purge
  nulls `indexed_at` so the next sync re-describes the image.

**Phase 2** (`tests/test_enrichment_runs.py`):
- a summarisation pass opens a run, stamps `documents.summary_run_id`, and
  closes the run with `status=finished`;
- a failed pass closes with `status=failed` and stamps nothing;
- the resolved model name is recorded, not the empty config default;
- purge filtered by `provider`/`model` touches only matching rows;
- `run_id IS NULL` filter selects exactly the pre-provenance rows.

`tests/conftest.py` already mocks the chat/vision providers, so no new
external boundary — no new fixture needed.

## 9. Docs

- `DESIGN.md` §3.2 — provenance is part of the enrichment ladder's contract
  once artifacts are stamped; add `enrichment_runs` to the schema discussion.
- `docs/ingestion-flows.md` — **needs updating in the same commit** for
  Phase 2, per `CLAUDE.md`'s sync rule: opening/closing a run around the
  summarisation call changes the shared tail (`attach_summary_chunk` is drawn
  in several graphs). Phase 1 is a read/delete surface over rows the pipelines
  already wrote and needs no graph change.
- `README.md` — the new `alexandria purge` subcommand beside `purge-source`.

## 10. Risks and open choices

- **Scope creep into a general "job history" system.** `enrichment_runs`
  looks like the start of one, and it should be resisted: it records model
  provenance for artifacts, not a task queue. `progress/` already owns
  live job state and must not be merged into this.
- **A purge that races an in-flight run** leaves stamped artifacts pointing at
  a `status=running` row. The `sp.is_running()` guard covers the sync path; a
  crashed process leaves a stale `running` row that nothing reaps. Either
  reap on startup or treat `running` older than N hours as `failed` — decide
  when implementing, do not leave it undefined.
- **`chunk_pass` is nullable and unset on older rows**, so
  `WHERE chunk_pass='summary'` will not match summary chunks written before
  that column landed. Check the actual distribution in a real archive before
  trusting the `summaries` target to be complete; a `NULL`-handling fallback
  may be needed for one release.
- **Open choice: does purging summaries also clear `card_summary`?** No — it
  is often the source-provided abstract (arXiv, bioRxiv, PubMed), not model
  output. But for a plain fetched page it *is* derived (`body_excerpt`). Tier
  boundary is genuinely blurry here; recommendation is to leave `card_summary`
  alone and revisit if it proves confusing.
- **Chunk reassembly quality (§5.2.1).** The enrich pass joins overlapped
  chunks back into summarisable text. The overlap is deterministic, so the
  join is mechanical — but verify against a real multi-chunk document that the
  result reads as prose and not as duplicated sentence fragments, since that is
  what the summariser will be handed. Retiring this entirely is what §5.2.2 is
  for.
- **Deleting vectors is not free in Chroma.** `drop_document_collection` +
  `rebuild_from_chunks` is the well-trodden path; per-id `purge_vectors` on a
  large id list is slower and is already the source purge's approach. Prefer
  the rebuild for anything archive-wide.
