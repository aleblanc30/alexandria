# Collection passthrough to tags

Promote the organisational structure the user already built in their source app
— Zotero collections, Firefox bookmark folders — into Alexandria's tag
namespace, so it becomes browsable and filterable alongside source tags and
cluster labels.

Covers the `TODO.md` item *"Use zotero collection names as tags"*, extended to
Firefox as the second (and arguably higher-value) source.

Not authoritative about current behavior — this is proposed work.

---

## 0. What already exists

The capture side is **already done**, which narrows this plan considerably.
`source_collections` (`pka/db/schema.py:61`) is written today by five sources:

| Source | What it writes | Where |
|--------|----------------|-------|
| Zotero | collection names (flat, one row each) | `runners/zotero.py:92,139` |
| Firefox | `folder_path`, slash-joined | `runners/firefox.py:68` |
| Calibre | series name | `runners/calibre.py:133,182` |
| Reddit | `r/<subreddit>` | `runners/reddit.py:124` |
| YouTube | playlist titles | `runners/youtube.py:53` |

So the work is **a projection into the tag namespace**, not new ingestion. Two
consequences worth stating up front:

- **No connector changes are required** for the core feature. `ZoteroItem.collections`
  and `FirefoxBookmark.folder_path` already carry what we need.
- **A backfill needs no re-ingestion.** Every existing archive already has the
  rows; tags can be derived from `source_collections` in place. This matters
  because running real ingestion is off-limits for agents working in this repo
  (`CLAUDE.md` § Boundaries), and it means the feature lands for the user's
  17.9k-doc library without a resync.

Today `source_collections` surfaces in exactly one place — the document detail
panel (`api/document_serialize.py:257`) — plus a Reddit fallback path
(`DESIGN.md` §353). It is not filterable, not countable, and not in `/tags`.

---

## 1. Where the tags live

**Decision: `overlay_tags` with a new `TagOrigin.COLLECTION = "collection"`.**

The alternative — appending to `source_tags` so collections show up under the
existing `origin=source` — is blocked by a concrete mechanism, not just taste:

> `insert_source_tags` (`db/queries.py:347`) deletes **all** `source_tags` rows
> for the `(document_id, source)` pair before inserting. `insert_source_collections`
> does the same for its own table. Two writers into one table on the same key
> would clobber each other, so Zotero's real user tags and its collection names
> could not both survive a run without merging the two call sites into one.

`overlay_tags` avoids this and is a better fit anyway:

- It has an `origin` column and a unique index on `(document_id, tag, origin)`,
  so a collection tag and an identically-named manual tag coexist.
- `sync_classification_tags` (`pka/classification.py:91`) is an existing,
  proven desired-vs-existing sync for exactly this shape — add-missing,
  delete-stale, scoped by origin. The collection sync is the same algorithm
  with a different tag set, so it should be written by generalising that
  function rather than duplicating it.
- Renaming a Zotero collection or moving a Firefox folder then *removes* the
  stale tag on the next run, instead of leaving both forever.

`source_collections` stays as-is. It remains the record of what the source
said; `overlay_tags` becomes the derived, queryable projection. Keeping both
means a normalisation-rule change is re-derivable without a resync.

---

## 2. Normalisation — the actual work

The two sources are shaped differently, and this is where the design decisions
are.

### Firefox: paths with structural roots

`folder_path` is a full slash-joined path built by `_build_folder_index`
(`connectors/firefox.py:73`), e.g. `"toolbar/Research/Distributed Systems"`.
It includes Firefox's structural roots (`menu`, `toolbar`, `unfiled`, `mobile`),
which are meaningless as tags and would each land on thousands of documents.

Rules:

1. **Strip structural roots.** Drop a leading segment matching Firefox's root
   set. Do this on the *segment*, not by string prefix, so a user folder
   genuinely named "Toolbar" nested deeper survives.
2. **Emit each remaining segment as its own tag.** `Research/Distributed Systems`
   → `Research` **and** `Distributed Systems`.
   - *Rationale:* filtering on `Research` should return the subfolder's contents
     too. Leaf-only loses the parent; full-path-only makes `Research` unmatched
     by anything nested under it. Segment expansion gives hierarchical roll-up
     for free using the flat tag filter that already exists.
   - *Cost:* tag count grows with folder depth. Mitigated by rule 4.
3. **Skip empty and whitespace-only segments** (`_build_folder_index` yields
   `""` for untitled folders and joins them into `//`).
4. **Cap depth** at a configurable number of segments from the root (suggest 4),
   so a pathological 12-deep tree does not spray twelve tags per bookmark.

### Zotero: flat names that should not be

The connector reads `collectionName` only (`connectors/zotero.py:279-286`).
Zotero collections **nest** via `collections.parentCollectionID`, and that
column is never read anywhere in the codebase — so today a nested collection
arrives as a bare leaf name with its parent lost.

Two options:

- **(a) Ship flat first.** Zotero collection names become tags verbatim, no
  hierarchy. Zero connector change.
- **(b) Reconstruct paths first**, mirroring `_build_folder_index`, then run
  the same segment-expansion as Firefox.

**Recommendation: (a) for the first cut, (b) as a follow-up.** Flat names are
already useful and unblock the shipped feature; path reconstruction is a
self-contained connector change that can land after, and the normaliser written
for Firefox will accept the paths unmodified when it does. Note that (b) is the
change that makes the two sources genuinely symmetric — worth doing, just not
worth coupling to this.

### Shared rules (both sources)

- **Trim** surrounding whitespace.
- **Drop single-character and empty** results.
- **Case:** preserve as written, but deduplicate case-insensitively within one
  document so `ML` and `ml` from two folders do not both land.
  Do *not* globally lowercase — these are user-authored proper nouns.
- **Do not** apply cross-document case merging here. That is the separate
  *"Deduplication of tags"* TODO item and should not be smuggled in.

Put all of this in one pure, well-tested function — no DB, no I/O:

```
pka/ingestion/collection_tags.py
    normalize_collection_tags(collections: list[str], source: Source) -> list[str]
```

Pure-function-with-a-table-of-cases is the whole risk surface of this feature;
everything else is plumbing.

---

## 3. Write path

One helper, called by both runners, alongside the existing
`_sync_*_classification` calls:

```
sync_collection_tags(doc_id: int, collections: list[str], source: Source) -> None
```

- normalises via §2,
- syncs `overlay_tags` at `origin="collection"` using the add-missing /
  delete-stale pattern generalised out of `sync_classification_tags`.

Call sites — all four already have `doc_id` and the collection list in hand,
immediately after the existing `insert_source_collections(...)`:

- `runners/zotero.py:92` (`ingest_zotero_items`) and `:139` (`ingest_zotero_metadata`)
- `runners/firefox.py:68` (`_persist`) — and check whether the embed-pass path
  needs the same, matching how `_sync_firefox_classification` is placed.

**Confine the first cut to Zotero and Firefox.** Calibre series, `r/<subreddit>`,
and YouTube playlists are all plausible follow-ups, but each has its own noise
profile (a subreddit tag duplicates information already on `reddit_items`), and
scoping by source keeps the blast radius small. The helper takes `source` so
adding one later is a one-line call, not a redesign.

---

## 4. Backfill

Because §0 holds, a backfill is a pure SQLite → SQLite pass with no connector
and no network:

```
alexandria tags backfill-collections [--source zotero|firefox] [--dry-run]
```

Read `source_collections` grouped by `(document_id, source)`, run the §2
normaliser, write via the §3 helper. Idempotent — the delete-stale sync means
re-running it converges rather than accumulating.

This is also the **only way to test the feature end-to-end against the real
library** without running ingestion, so it is not optional polish. Ship it with
the feature, and make `--dry-run` report the tag count it *would* create,
per source, so the user can sanity-check the noise level on 17.9k documents
before committing rows.

---

## 5. API and frontend surface

Backend:

- `pka/constants.py` — add `COLLECTION = "collection"` to `TagOrigin`.
- `db/queries.py:1215` — add `str(TagOrigin.COLLECTION)` to the `overlay_origins`
  set in `list_tags`, or the new tags are invisible to `/tags` even though the
  rows exist. **This is the easiest step to miss**: the union query already
  reads all of `overlay_tags`, so the tags are filtered out at the origin gate,
  silently.
- `db/queries.py:927` — add `collection_tag_filter` to
  `_apply_document_browse_filters`, delegating to the existing
  `_where_overlay_tag(q, tag, TagOrigin.COLLECTION)`. No new SQL shape.
- `api/routers/tags.py` and the browse/search routers — add the
  `collection_tags` query param alongside `cluster_l1_tags` / `cluster_l2_tags`,
  which it mirrors exactly.

Frontend:

- `api/client.ts` — `collection_tags?: string[]` in the three param interfaces
  and their `qs.append` loops (`:251`, `:313` and the search variant).
- `stores/browse.ts` — a `collectionTags` ref + toggle, mirroring `sourceTags`
  (`:29`, `:205`).
- Tag facet UI — a "Collections" group. Given a deep folder tree can produce a
  long facet list, reuse whatever truncation the existing tag facets use rather
  than inventing one.

---

## 6. Config

```python
collection_tags_enabled: bool = True     # pka/config.py
collection_tag_max_depth: int = 4
```

Default **on**: this is a purely local derivation of data already in the
archive, so `DESIGN.md` §1.1's named-setting-default-off rule does not apply —
that governs *outbound* paths, and this makes no calls. The setting exists so a
user who finds folder tags noisy can turn them off, and so the backfill and the
runners agree on one switch.

---

## 7. Tests

- `normalize_collection_tags` — table-driven, and this is where the coverage
  should concentrate: root stripping, segment expansion, the "Toolbar nested
  deeper" case, empty/`//` segments, depth cap, per-document case dedup,
  Zotero flat names passing through untouched.
- Sync semantics — new tag added; renamed collection removes the stale tag and
  adds the new one; re-running is a no-op; a `manual` tag with the same string
  survives a collection sync (the origin scoping is the whole point).
- `list_tags` returns `origin="collection"` rows, and respects
  `origin="collection"` as a filter.
- Browse filter by `collection_tags` narrows correctly, including combined with
  a source filter.
- Backfill — populates from `source_collections`, is idempotent, respects
  `--source`, and `--dry-run` writes nothing.
- Frontend: `npm run test` + `npm run build`.

---

## 8. Docs to update in the same commit

Per `CLAUDE.md`, both derived docs have sync rules that this change trips:

- **`docs/persisted-fields.md`** — a new `overlay_tags` origin is a change to
  *what a source writes*; the Zotero and Firefox columns move. The file's value
  is that a `—` can be trusted.
- **`docs/ingestion-flows.md`** — the Zotero and Firefox graphs gain a node next
  to the existing `insert_source_tags()` / `insert_source_collections()` boxes
  (`:169`, `:249`). Shared-vs-source-specific colouring: the normaliser and sync
  helper are **shared**, so they take the shared colour even though only two
  pipelines call them.
- **`DESIGN.md`** — a sentence in the tag/overlay section on the new origin and
  what it derives from.
- **`TODO.md`** — tick *"Use zotero collection names as tags"*.

---

## 9. Phasing

1. `normalize_collection_tags` + tests (pure, no wiring — the whole risk).
2. `sync_collection_tags` + generalise `sync_classification_tags`'s sync loop.
3. Wire the four runner call sites; `TagOrigin.COLLECTION`.
4. `list_tags` origin gate + browse filter + API params.
5. Backfill CLI with `--dry-run`.
6. Frontend facet.
7. Docs (§8).

Follow-ups, deliberately out of scope: Zotero path reconstruction (§2b),
Calibre/Reddit/YouTube passthrough (§3), cross-document tag case merging (the
separate dedup TODO).

---

## 10. Open questions

- **Segment expansion vs leaf-only** (§2, rule 2) is the one call worth
  confirming against the real library before building the facet. `--dry-run`
  on the backfill answers it with counts: if `Research` lands on 4k documents,
  it is a source filter, not a tag.
- **Should a collection tag participate in clustering or tag training?**
  `overlay_tags` rows are read by the tag-training path; a sudden influx of
  high-frequency folder tags may change its behaviour. Worth checking
  `pka/tag_training/` before step 3 rather than after.
