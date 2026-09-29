# Item deduplication

Detect when two `documents` rows are the same work saved twice — the same URL
bookmarked and also filed in Zotero, the same paper reached through arXiv and
through the publisher, the same book in Calibre and on a shelf photo — and
collapse them into one browsable, searchable, clusterable item without losing
either source's record of it.

Covers the `TODO.md` item *"Deduplication of items"* under *Ingestion &
deduplication*. The sibling item *"Deduplication of tags"* is a different
problem (string canonicalisation inside one namespace) and is not covered here.

Not authoritative about current behavior — this is proposed work.

**Outcome (archived).** Shipped as designed, all phases at once, recorded in
`DESIGN.md` §3.9. Differences: the embedding near-duplicate pass (§11) shipped
as review-only candidates; linked pairs union their tags and sources at read
time (the §11 open question, decided yes); the scan lives in `pka/dedupe.py`
and the read helpers in `pka/db/duplicates.py` rather than a `pka/dedupe/`
package, for the import layering; review is on the Ingestion page rather than
inside the Maintenance panel. Still open, not planned: preprint ↔ published
as a "version of" relation, compaction of a duplicate's chunks, and skipping
enrichment for linked duplicates.

---

## 0. What already exists

`archive/DOCUMENT_METADATA_PLAN.md` shipped the join keys, which is why this is
now worth planning:

| Column | Normalisation | Written by |
|--------|---------------|------------|
| `doi` | `identifiers.normalize_doi` — prefix stripped, lowercased | Zotero runner (`runners/zotero.py:67`), every identifier-resolving fetch handler via `FetchResult.doi` (`fetcher.py:673`) |
| `arxiv_id` | `arxiv.normalize_arxiv_id` — no version suffix | same two paths |
| `isbn` | `openlibrary.normalize_isbn` + checksum | Calibre runner (`runners/calibre.py:67,124,174`), ISBN fetch handlers |

All three are indexed (`schema.py:51-53`, added by P-1, whose comment already
says *"Join keys for cross-source dedup"*). `documents` is unique on
`(source, source_id)` (`uq_source_item`), so there is no such thing as a
same-source, same-id duplicate: everything this plan is about is either
cross-source, or same-source under two different source ids.

Two coverage facts shape the phasing, and both argue that identifiers alone will
not find much:

- **Firefox and Reddit runners write no identifiers at all.** A Firefox
  bookmark gains a DOI only if it was fetched *and* its host has a handler
  (arXiv, bioRxiv, PubMed, doi.org, Nature, Springer, APS, ScienceDirect, MIT
  Press). A pending, unfetchable, or ordinary-blog bookmark has `doi IS NULL`
  forever.
- **arXiv preprints and their published versions carry different DOIs.**
  `derive_arxiv_doi` mints `10.48550/arxiv.<id>`, which never equals the
  journal's `10.1103/...`. Identifier equality will not unify a preprint with
  its published record, and should not pretend to.

So the identifier keys catch Zotero ↔ fetched-publisher-bookmark and Calibre ↔
book-cover-photo. The everyday case — the same URL saved twice — needs a URL
key, which does not exist yet.

There is no URL canonicalisation anywhere in the codebase. `domains.py`'s
`extract_domain` lowercases the host and strips `www.`, and that is the whole of
it. `fetcher._throttle_key` is a rate-limit bucket, not an identity.

---

## 1. What counts as a duplicate

Match keys, strongest first. Each produces candidate pairs; §4 decides what
happens to a pair.

| Key | Rule | Auto-link? |
|-----|------|------------|
| `arxiv_id` | exact, post-`normalize_arxiv_id` | yes |
| `doi` | exact, post-`normalize_doi`, **plus** the bridge below | yes |
| `isbn` | exact after converting both sides to ISBN-13 | yes |
| `url` | exact after `canonical_url` (§2) | yes when hosts match |
| embedding | cosine over `documents.doc_embedding` above a threshold | no — deferred, see §11 |

**The arXiv bridge.** A Zotero item may carry `arxiv_id` and no DOI while a
fetched arXiv bookmark carries the derived DOI and no `arxiv_id`, or the
reverse. Compare on `resolve_doi(doi, arxiv_id)` (`ingestion/identifiers.py:29`)
rather than on the raw column, so both rows reduce to the same string. This
needs no new logic, only using the helper that already exists.

**ISBN-13 conversion is match-only.** `normalize_isbn` accepts both 10 and 13
characters and converts neither, so the same book can sit in the archive as
`0262035618` and `9780262035613`. Add `isbn13(raw)` to
`ingestion/openlibrary.py` next to the existing checksum helper and use it for
the key. Do **not** rewrite `documents.isbn`: it records what the source said,
and `docs/persisted-fields.md` describes it that way.

**Editions are not duplicates.** Two ISBNs for the same title (paperback and
hardback, or two translations) are different items and will not match, which is
correct. Nothing here should try to merge on title similarity.

---

## 2. `canonical_url`

New module `pka/dedupe/keys.py`. Pure functions, no I/O, no config.

```
canonical_url(url) -> str | None
```

- non-`http(s)` returns `None` (a Calibre path or `zotero://` link has no URL identity)
- scheme forced to `https`, host lowercased, `www.` and `m.` prefixes stripped
- fragment dropped; default ports dropped; trailing slash stripped
- tracking parameters dropped: `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `igshid`, `si`, `ref`, `ref_src`
- remaining query parameters sorted by key, so parameter order stops mattering
- per-host rules, each a small function so a new host is one entry, mirroring
  how `domains.domain_has_fetch_handler` composes its predicates:
  - youtube: `youtube.com/watch?v=<id>`, absorbing `youtu.be/<id>`,
    `m.youtube.com`, and any `t`/`list`/`index` parameters
  - reddit: the `/r/<sub>/comments/<id>/` prefix, dropping the trailing slug
    and any `?context=`
  - amazon: `/dp/<ASIN>` only, dropping the SEO slug and every regional query
    parameter (the host TLD stays — `.fr` and `.com` are different listings, and
    `is_amazon_host` already treats them as one host family for fetching, not
    for identity)

**This key is for matching only and must never be fetched.** A canonicalised URL
can 404 where the original works. Keeping it out of `documents.url_or_path`
keeps that rule impossible to break by accident.

Tested as a table of `(raw, expected)` pairs in a new `tests/test_dedupe_keys.py`,
including the negative cases: two different YouTube videos, two Amazon ASINs,
`?page=2` surviving the strip.

---

## 3. Merge or link

**Decision: link. Nothing is deleted, and both rows keep their own children.**

The alternative — pick a winner, reparent its children, delete the loser — is
not just riskier, it does not survive a sync. `insert_document_if_new` and
`upsert_document` key on `(source, source_id)` (`db/queries.py:288-332`), so a
deleted Firefox row is re-inserted with a fresh id by the next
`alexandria firefox` run, re-fetched, re-embedded, and re-summarised. A hard
merge would silently undo itself and pay for the enrichment again on every
sync, unless a tombstone table recorded the suppressed `(source, source_id)`
pairs — which is the same amount of new state as a link table, with data loss
added on top.

Linking also gives unmerge for free (one `DELETE`), which matters because the
first scan over a real 17.9k-document archive will get some pairs wrong.

The cost is accepted explicitly: a linked duplicate keeps its chunks and its
Chroma vectors, so the archive stores the same text twice. §7 lists an optional
compaction pass if that turns out to matter; it is not part of the feature.

---

## 4. Schema

One table, no new `documents` column:

```python
document_duplicates = sa.Table(
    "document_duplicates", meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("canonical_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("duplicate_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("match_key", sa.Text, nullable=False),   # doi|arxiv_id|isbn|url|manual
    sa.Column("match_value", sa.Text),                 # the shared key, for the review UI
    sa.Column("state", sa.Text, nullable=False),       # candidate|merged|rejected
    sa.Column("decided_by", sa.Text),                  # auto|user
    sa.Column("decided_at", sa.Integer),
    sa.UniqueConstraint("canonical_id", "duplicate_id", name="uq_dup_pair"),
    sa.Index("ix_document_duplicates_canonical_id", "canonical_id"),
    sa.Index("ix_document_duplicates_state", "state"),
    sa.Index(
        "uq_document_duplicates_merged",
        "duplicate_id",
        unique=True,
        sqlite_where=sa.text("state = 'merged'"),
    ),
)
```

Three properties the design leans on:

- **The partial unique index is the no-two-canonicals invariant.** A row can be
  a *candidate* against several canonicals but `merged` under exactly one. If
  the partial index proves awkward under SQLAlchemy's SQLite dialect, enforce it
  in the writer and keep the test either way.
- **`rejected` rows are permanent.** A rejected pair is how the scan remembers
  that the user already said no, so re-running it does not re-ask. This is the
  reason candidates and decisions share one table instead of the scan writing to
  a scratch table.
- **No chains.** The writer refuses a link whose `canonical_id` is itself a
  merged `duplicate_id`, flattening to that row's canonical instead. Read sites
  can then treat the map as one hop, never a walk.

Migration: `meta.create_all` handles the new table, so `init_db` needs no
`ALTER` block. `tests/test_schema_migration.py` gets a case asserting the table
and its indexes appear on an archive created before this shipped.

Why no `documents.duplicate_of` column: it would be a second source of truth
that a manual `DELETE` from the link table could leave stale, and the EXISTS
against an indexed table is the same shape as `_exclude_pending_images`
(`db/queries.py:1031`), which is the established pattern for hiding rows from
browse.

---

## 5. Choosing the canonical row

Deterministic, so a re-scan never flips canonicality (which would churn the read
path for no reason). Order a candidate group by, in sequence:

1. `fetch_status` rank: `fetched`/`available` > `no_text_layer` > `pending`/`skipped` > `unfetchable`/`missing`
2. chunk count, descending — the row that actually carries the text wins
3. `ingested_at`, ascending — the row the archive has held longest
4. `id`, ascending — final tiebreak, never reached in practice

Deliberately **not** a source preference. Zotero carries better bibliography and
Firefox carries the live URL, and neither is uniformly the better row; the
richness ordering above picks whichever actually has content, which is what the
read path needs.

---

## 6. What the read path does

The link table is inert until these four sites consult it. One helper backs all
of them, in a new `pka/dedupe/resolve.py`:

```
merged_duplicate_ids(con) -> set[int]      # cached per request/run, small
exclude_duplicates(q: sa.Select) -> sa.Select   # correlated EXISTS, browse-shaped
canonical_id_map(con, doc_ids) -> dict[int, int]
```

| Site | File | Change |
|------|------|--------|
| Browse list + count | `db/queries.py:1126` (`list_documents`) | wrap both queries in `exclude_duplicates`, exactly as `_exclude_pending_images` is applied today |
| Search results | `api/routers/search.py:46-52` | fold hits through `canonical_id_map` before the `seen` dict, so a duplicate's chunk scores the canonical row instead of adding a second card; the dict already keeps the max, so no new tie logic |
| Clustering corpus | `clustering/embeddings.py:67-74` | subtract `merged_duplicate_ids` from `candidate_doc_ids`; the corpus is built from Chroma metadata, so this is the only place it can be done |
| Tag-training corpus | `tag_training/engine.py:93,102,211` | same subtraction, so a duplicate is never queued for labelling and never counted twice in `train_stats` |

Everything else is deliberately left alone, and the reasons are worth recording
so a later reader does not "fix" them:

- **Ingestion counts and progress** (`ingestion/progress/baselines.py`,
  `pending_metadata.py`) must keep counting duplicates. They report what the
  source contains; hiding a row there would make the sync look like it lost
  items.
- **`domains.py`** ranks hosts by document count. A duplicate URL genuinely was
  saved twice, and the report exists to prioritise handlers.
- **`purge-source`** already deletes by source and would leave a dangling link
  row. Add `document_duplicates` to `_CHILD_TABLES` in `cli/purge_source.py:45`
  keyed on both columns, the same way the other child tables are handled.
- **Past cluster runs** keep their `cluster_assignments` and `umap_points` for
  rows that are now duplicates. A run is a historical record of a corpus, and
  `scatter_points` (`api/routers/clusters.py:200`) joins titles by id, so those
  points still render. Only the *next* run's corpus is filtered.

---

## 7. Chroma, and what is deliberately not touched

Chunk vectors carry `document_id` in their metadata (`ingestion/core.py:68`) and
vector ids are UUIDs unrelated to the document (`core.py:64`), so nothing about
linking requires a Chroma write. Search filtering happens after the query, in
Python, on the collapsed map.

An optional later compaction — delete the duplicate's chunk rows and
`purge_vectors` its vector ids — would halve the storage of a linked pair, but
it makes unmerge require a re-fetch and re-embed. It is not part of this
feature; if it is ever wanted, it belongs as a purge target in
`PURGE_AND_PROVENANCE_PLAN.md`'s `TARGETS` map, not as a special case here.

`refresh_document_embedding` needs no change: it is per-document and the
duplicate's own embedding stays valid and simply goes unread.

---

## 8. The scan

`pka/dedupe/scan.py`, no network, no model calls — this feature makes no
outbound requests in any configuration, so `DESIGN.md` §1.1 has nothing to say
about it.

- Identifier keys: three `GROUP BY ... HAVING COUNT(*) > 1` queries over the
  indexed columns, plus a fourth pass reducing `(doi, arxiv_id)` through
  `resolve_doi` for the bridge in §1.
- URL key: one pass over `SELECT id, url_or_path, fetch_status, ingested_at`,
  bucketed by `canonical_url` in Python. At archive scale (~18k rows) this is a
  fraction of a second and needs no index.
- Group, not pair: union-find over all candidate edges, then §5 picks the
  canonical and every other member becomes a `duplicate_id` against it. This is
  what keeps a three-way duplicate from producing a chain.
- Pairs already `merged` or `rejected` are skipped.

Output is a `dict[str, int]` count summary in the same shape the purge targets
return, so the CLI and any later API can print it identically.

---

## 9. Surfaces

Phase order in §10; this is what each surface looks like when it lands.

```
alexandria dedupe scan [--key doi,arxiv_id,isbn,url] [--json]
alexandria dedupe list [--state candidate] [--limit N]
alexandria dedupe apply [--key ...] [--dry-run]      # candidate -> merged
alexandria dedupe reject <duplicate_id>
alexandria dedupe unmerge <duplicate_id>
alexandria dedupe link <canonical_id> <duplicate_id> # match_key='manual'
```

Registered in `cli/__init__.py`'s `COMMANDS` as `"dedupe": ("dedupe", "Find and
link duplicate documents")`, alongside the three purge commands it is a sibling
of.

API and UI reuse the Maintenance panel that `/ingestion/purge*` already
populates: `GET /dedupe/candidates`, `POST /dedupe/apply`, `POST /dedupe/reject`,
`DELETE /dedupe/{duplicate_id}`. The review list is a table of candidate pairs
showing both titles, both sources, the match key and its value.

The document detail panel gains an "also saved in" line listing the duplicate's
source and title — the visible payoff of the whole feature, and the reason a
linked duplicate is worth more than a deleted one.

---

## 10. Phases

**Phase 0 — measure, no writes.** `canonical_url` + the scan + `dedupe scan
--json`. Nothing is persisted; the command prints how many groups each key
finds. This exists because the rest of the design should be sized by real
numbers, and §0 predicts the identifier keys find little while the URL key finds
most of it. If Phase 0 says otherwise, revisit §1 before building §4.

**Phase 1 — link and unlink, CLI only.** The table, the writer with its
flattening and invariant checks, `apply` / `reject` / `unmerge` / `link`,
`purge-source` cleanup. Read sites untouched, so nothing user-visible changes
yet and the links can be inspected in SQL before they take effect.

**Phase 2 — read-path collapse.** The four sites in §6, plus the detail-panel
"also saved in" line. This is the phase that changes what the user sees.

**Phase 3 — review UI**, if Phase 0's numbers make the URL key's candidate
volume too large to work through in the CLI.

**Later, separately** — skipping fetch and summarisation for a row already
linked as a duplicate. Worth real money on a billable provider, but note where
the check goes: in the enrich pass's queue query (`ingestion/enrich.py`), not in
`ingest_text_block`. Putting it in the shared tail would change the shape drawn
in all seven graphs of `docs/ingestion-flows.md`; keeping it in the queue does
not.

---

## 11. Open questions

- **Embedding near-duplicates.** `documents.doc_embedding` makes a cosine scan
  cheap, and it is the only key that would catch a PDF saved under two unrelated
  URLs. It also confidently pairs the papers of one series. Deferred until
  Phase 0 shows how much the exact keys miss; if it lands, it is candidates-only,
  never auto-linked.
- **Preprint ↔ published.** §1 says identifier equality cannot join them. Whether
  they *should* be one item is a genuine question — the two have different text,
  and a reader may want the published version specifically. Leaning towards a
  separate relation ("version of") rather than overloading duplication.
- **Whether a linked pair should merge its tags.** Currently the canonical shows
  its own tags only. Unioning `source_tags` at read time would make browse
  filters find the canonical through the duplicate's Zotero tags, which is
  probably wanted, but it interacts with the tag-index counts in `list_tags` and
  is a decision to make with Phase 2 in hand.

---

## 12. Tests and docs

New: `tests/test_dedupe_keys.py` (the §2 table, both polarities),
`tests/test_dedupe_scan.py` (each key, the arXiv bridge, three-way grouping,
rejected pairs staying skipped), `tests/test_dedupe_merge.py` (canonical
ordering, the no-chain flattening, the partial-unique invariant, unmerge round
trip). Extend `tests/test_db.py` for browse exclusion, `tests/test_api.py` for
search collapse, `tests/test_schema_migration.py` for the new table,
`tests/test_purge_source.py` for link cleanup.

`docs/persisted-fields.md` needs the new table in its side-table matrix and a
note that `document_duplicates` is written by no source — it is a maintenance
artifact, like `cluster_runs`. `docs/ingestion-flows.md` needs **no change** in
phases 0-2: nothing here sits in a pipeline phase, crosses the shared/
source-specific line, or adds an outbound call. `DESIGN.md` gains a short
subsection under §3 describing the link table and the four read sites that
honour it.
