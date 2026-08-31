# Structured document metadata in SQLite

**Status:** proposed, not implemented.
**Unblocks:** `planning/TODO.md` → *Deduplication of items* (needs a DOI it can join on).
**Touches:** `pka/db/schema.py`, `pka/db/queries.py`, `pka/db/init_db.py` (via
`queries.init_db`), `pka/connectors/zotero.py`, `pka/ingestion/runners/zotero.py`,
`pka/ingestion/zotero_sync.py`, `pka/ingestion/runners/calibre.py`,
`pka/ingestion/arxiv.py`, `pka/ingestion/biorxiv.py`,
`pka/api/schemas/documents.py`, `pka/api/document_serialize.py`, `DESIGN.md`,
tests. Reuses `normalize_arxiv_id` and `normalize_isbn` / `isbn_checksum_valid`
rather than adding canonicalisers.

## The problem

Every runner flattens its source's structured metadata into one prose blob on
the way into `ingest_text_block`, and that blob is the only copy that survives:

| Source | Parsed by the connector | Persisted | Ends up in |
|---|---|---|---|
| Zotero | authors, DOI, year | — | `zotero_embed_text` → chunk text |
| Calibre | authors, year, ISBN | — | `metadata_text` → chunk text |
| arXiv | authors, arXiv ID | — | `build_preprint_text` → chunk text |
| bioRxiv | authors, DOI | — | `build_preprint_text` → chunk text |

`_zotero_document_kwargs` persists source, id, title, url, date, fetch status,
attachment key and item type — and that is all. `CalibreBook` parses `year` and
`isbn` into a dataclass that then drops them on the floor.

**Flattening into prose is not itself the mistake.** "by Smith, Jones" in the
embed text genuinely helps retrieval — someone searching for "that Smith paper
on consensus" should hit it, and that text stays exactly as it is. The mistake is
that the blob is doing double duty as the system of record, and it is bad at that
job: the chunker whitespace-normalises, sentence-windows and overlaps, so
recovering a field means regexing prose that has already been mangled.

The concrete cost is a feature already on the list. *Deduplication of items*
wants to merge on "same URL, DOI, arXiv ID". URL is a column; DOI is prose in
three sources. That pass cannot be written today.

## Decision

Add the shared bibliographic fields to `documents` as nullable columns. **No
per-source sidecar table.**

The sidecar (`zotero_items`, matching `images` / `reddit_items`) was considered
and rejected: it would solve Zotero only, leaving Calibre authors and bioRxiv
DOIs equally stranded, and a cross-source dedup would then have to union across
sidecars that do not all exist. These fields are not source-specific — DOI spans
Zotero and bioRxiv, authors span all four, year spans Zotero and Calibre.

`documents` already has the convention for this. `zotero_attachment_key` is a
single-source column sitting in it; `archive_url` is Firefox-only; `item_type` is
commented "Zotero itemTypes.typeName" and is read by Reddit for post-vs-comment.
Nullable, populated by whoever has it, is how that table already works.

### Schema

```python
sa.Column("doi",          sa.Text),     # bare DOI, no https://doi.org/ prefix
sa.Column("arxiv_id",     sa.Text),     # normalize_arxiv_id form — no version suffix
sa.Column("isbn",         sa.Text),     # normalize_isbn form — digits/X, no hyphens
sa.Column("year",         sa.Integer),  # publication year — not date_added
sa.Column("authors_json", sa.Text),     # JSON array of strings, order preserved
sa.Column("zotero_url",   sa.Text),     # Zotero item `url` field, verbatim
sa.Column("zotero_path",  sa.Text),     # resolved local attachment path
```

plus an index on each of the three identifiers — being a join key is the whole
point of them:

```sql
CREATE INDEX IF NOT EXISTS ix_documents_doi      ON documents(doi);
CREATE INDEX IF NOT EXISTS ix_documents_arxiv_id ON documents(arxiv_id);
CREATE INDEX IF NOT EXISTS ix_documents_isbn     ON documents(isbn);
```

### Identifiers: three columns, canonical forms, no new machinery

The dedup TODO names "same URL, DOI, arXiv ID" and books need the same treatment,
so all three identifiers land together. None of them requires anything new to be
written — the canonical forms already exist and are already tested:

| Column | Canonicaliser | Notes |
|---|---|---|
| `doi` | — (store lowercased, prefix-stripped) | `10.1145/…`, never `https://doi.org/…` |
| `arxiv_id` | `normalize_arxiv_id` (`pka/ingestion/arxiv.py`) | strips the `v2` suffix, so versions of one paper collapse |
| `isbn` | `normalize_isbn` + `isbn_checksum_valid` (`pka/ingestion/openlibrary.py`) | hyphens out; reject a failed checksum rather than storing a typo as a join key |

Canonicalising **on write** is the point. A dedup pass that has to normalise at
query time is one that will eventually compare `978-0-13-235088-4` against
`9780132350884` and call them different books.

A paper commonly has **both** a DOI and an arXiv ID — that is the normal state of
a preprint, not a conflict. The columns are parallel facts, not alternatives, and
dedup may match on either.

**arXiv documents derive their DOI.** arXiv mints `10.48550/arXiv.<id>` for every
submission, so when `arxiv_id` is set and no DOI came from the source, write
`doi = "10.48550/arxiv." + arxiv_id` (lowercased like every other DOI, since DOIs
are case-insensitive and the column is a join key). This is a value *we*
synthesise rather than one the API returned, which is worth saying out loud —
but it is a deterministic, reversible function of a field we already store, so it
adds no claim that `arxiv_id` did not already make. The payoff is that preprints
stop being a hole in every DOI-keyed path: dedup and any future DOI-based lookup
work uniformly instead of needing an arXiv special case.

A source-provided DOI always wins. A published paper's Zotero record carries the
*journal* DOI, which is a different and better identifier than the arXiv one;
never overwrite it with the derived form.

**Why columns rather than a `document_identifiers(document_id, scheme, value)`
table.** The generic table is a real option and — unlike the rejected
`zotero_items` — would not contradict the sidecar decision, since the objection
there was source-specificity and this would be cross-source by construction. It
is simply not worth it at three schemes: direct columns index directly, and the
dedup pass reads more clearly as three explicit matches than as a self-join over
a scheme string. The trigger for revisiting is a **fourth and fifth** scheme
(PMID, ISSN) actually being wanted — worth watching for rather than
rediscovering.

Note `chunks.source_ref` already holds an ISBN when the §3.2 enrichment ladder
ran. `documents.isbn` becomes the canonical home; `source_ref` stays what it is,
enrichment provenance recording which identifier produced a given synopsis chunk.

`authors_json` rather than a `document_authors` join table: a join table would
make "papers by X" a real query, but `LIKE` over the JSON plus the author list
already in the embed text covers what is actually being asked, and the column is
a tenth of the work. `_json` matches the existing `images.books_json`. Store a
JSON array even for a single author so readers never have to guess the shape.

`year` is deliberately separate from `date_added`: when you saved something is
not when it was published, and the browse date filters mean the former.

## Splitting `zotero_document_url_or_path`

Today one function collapses four different facts into `documents.url_or_path`
by priority — http URL, else PDF path, else DOI, else raw URL. A journal article
with both an article URL *and* a PDF attachment keeps only the URL, so the
archive cannot answer "which Zotero items have a PDF on disk" without re-reading
Zotero.

Split it: `zotero_url(item)` and `zotero_path(item)` in the connector, stored in
the two columns above. **Columns hold facts, the ladder holds policy** — store
the `url` field verbatim (even when it is not http, which the current last rung
was there to catch) and the resolved attachment path independently, then let
`url_or_path` be computed from them.

`documents.url_or_path` keeps its cross-source meaning and its current
url-before-path preference, so browse, the frontend link, `filter_document_ids`
and purge are unaffected. One rung goes away:

**The DOI rung should be dropped**, now that `documents.doi` exists. Storing a
bare DOI string in a column the frontend renders as a link was always a
compromise, and the API can serve `https://doi.org/<doi>` from the real column
instead — strictly better than what is there now.

### What dropping that rung actually does to existing rows

The affected set is narrow: Zotero items with **no URL and no PDF attachment but
a DOI** — hand-entered conference papers and book chapters, mostly. Today those
rows hold a bare `10.1145/…` string in `url_or_path`. Under the new ladder the
same item computes to `NULL`.

The trap is that **no routine sync rewrites those rows**, so the archive ends up
holding both answers at once:

| Path | Writes `url_or_path` on an already-archived item? |
|---|---|
| `alexandria zotero` metadata phase | **No** — `insert_document_if_new` returns `None` on an existing `(source, source_id)` and updates nothing |
| `alexandria zotero` embed phase | **No** — `ingest_zotero_embed` calls `upsert_document` only when the item is absent from `document_index` |
| `alexandria zotero --force-reindex` | **Yes** — `skip_existing=False` empties `doc_ids`, so every item takes the `upsert_document` branch |
| `refresh_zotero_attachment_keys` | No — a targeted `sa.update()` touching one column |

So after shipping the split, a DOI-only item added *last year* keeps its bare DOI
string while the identical item added *tomorrow* gets `NULL`. Two rows, same
situation, different values, and nothing reconciles them until someone happens to
run `--force-reindex` — which then blanks the old ones in bulk, at a moment
unrelated to this change. That silent split-brain is worse than a visible
migration, and it is the actual consequence to design for.

`upsert_document` is what makes the `--force-reindex` case a blanking rather than
a no-op: `url_or_path = excluded.url_or_path` in the `ON CONFLICT` clause, with
no `COALESCE`, unlike the `item_type` / `note` / attachment-key assignments
beside it. That asymmetry is correct — `url_or_path` is genuinely recomputed from
the source each time — but it means "the new ladder returns `NULL`" and "the
stored value is erased" are the same event.

**Therefore:** have `refresh_zotero_metadata` recompute and write `url_or_path`
for every item it sees, not just the five new columns. It already runs over the
full item list on every metadata sync, which converts the drift into a one-time,
deliberate migration that lands with the change instead of ambushing a later
`--force-reindex`. And ship the API-side `https://doi.org/<doi>` link in the same
commit, so the affected items gain a working link in the same run that removes
the non-working string.

## Population, by source

Phase the *writers*, not the schema — the columns are cross-source from the
start, which is the entire argument against a sidecar.

| Source | doi | arxiv_id | isbn | year | authors_json |
|---|---|---|---|---|---|
| Zotero | ✅ `fields["DOI"]` | ✅ `parse_arxiv_url(item.url)` | — | ✅ `_parse_year` | ✅ |
| Calibre | — | — | ✅ `book.isbn` | ✅ `book.year` | ✅ `book.authors` |
| bioRxiv | ✅ `meta.doi` | — | — | — | ✅ |
| arXiv | ✅ *derived* | ✅ `meta.arxiv_id` | — | — | ✅ `meta.authors` |

Almost all of this is already parsed by the connector and thrown away today —
`CalibreBook.isbn` and `book.year` most flagrantly. This is plumbing, not new
extraction. The two exceptions are computed from fields already stored, by
functions already in the tree: the derived arXiv DOI, and Zotero's `arxiv_id` via
`parse_arxiv_url(item.url)`.

**Zotero's `arxiv_id` is what makes the identifiers earn their keep.** It is the
row that turns three columns into a cross-source join: a preprint saved in Zotero
and the same paper bookmarked and fetched from arxiv.org now agree on `arxiv_id`
*and* — through the derivation rule — on `doi`, which is precisely the pair the
dedup TODO cannot currently tell apart.

That also means the derivation is reached from more than one runner, so it belongs
in **one shared helper** rather than being written out per source. Anywhere
`arxiv_id` is set, the same "no source DOI → derive, source DOI wins" rule runs.

Two boundary details that will otherwise bite:

- `BiorxivMetadata.authors` is a **`str`**, not a `list[str]` like every other
  source. Normalise at that boundary rather than letting a bare string reach a
  JSON-array column.
- Zotero's arXiv coverage is **partial by construction**: `_FIELD_NAMES` loads
  `url` but not `Extra`, and Zotero's own arXiv translator often records the ID
  as `arXiv:2301.12345` in `Extra` while `url` points at the abs page or nothing.
  So this catches items whose `url` is an arXiv link and silently misses the rest.
  That is a fine place to start — it is free — but do not let the column's
  existence imply the archive knows every Zotero preprint's ID.

**Images are out of scope.** They carry no DOI, arXiv ID or ISBN of their own,
and cover extraction is a different mechanism with a different shape (several
books per photo, already held in `images.books_json`). Nothing in this plan
touches the image pipeline.

Preprint metadata is written from the fetcher rather than a metadata loop, which
is why Zotero and Calibre come first.

## Migration and backfill

**Migration** is the established `init_db()` pattern: `PRAGMA table_info` guard
then `ALTER TABLE documents ADD COLUMN`, plus `CREATE INDEX IF NOT EXISTS` for
each identifier index (`create_all` skips indexes on tables that already exist).
Seven columns, three indexes, no data rewrite; `alexandria init` stays idempotent.

**Backfill already has a mechanism, and it is not the one you would guess.**
`run_metadata_loop` skips items in `known`, so an existing archive would never
revisit its Zotero rows. The obvious fix — `skip_when_in_known=False`, as Reddit
uses — is wrong here: that flag also makes the loop tick progress for
already-archived items, which the loop's own comment says double-counts against
the baseline it started from. Reddit accepts that because it has no alternative.

Zotero does. `sync_zotero_metadata` already loads *every* item (`load_items()`,
not just pending ones) and already runs a post-loop backfill over all of them —
`refresh_zotero_attachment_keys`, which exists for exactly this reason and is a
plain per-item `sa.update()`, easy to widen. Generalise it to
`refresh_zotero_metadata(by_source_id: dict[str, dict])` writing doi, year,
authors_json, zotero_url, zotero_path **and the recomputed `url_or_path`** (see
the split section above — that last one is what keeps old and new rows from
disagreeing). The backfill then costs one changed call site that already exists.

Two different write semantics, on purpose.

The five new columns are `COALESCE`d — `x = COALESCE(incoming.x, stored.x)`, i.e.
take the new value unless it is NULL. This is not special caution about DOIs; it
is the rule the existing columns already follow, for a structural reason. All six
sources share one `upsert_document`, so a column that only some callers populate
arrives as `None` from everyone else, and an unconditional write would let a
Reddit or Calibre upsert blank a field it has never heard of. That is precisely
why `zotero_attachment_key`, `item_type` and `note` are `COALESCE`d today while
`title`, `url_or_path` and `fetch_status` — which every runner passes — are not.
For `doi` the collision is concrete rather than hypothetical: a bioRxiv paper *is*
a Firefox document, so the preprint fetch writes a DOI that any later
Firefox-side upsert would erase.

The cost, recorded because it is permanent: a `COALESCE`d field cannot be
*cleared* from the source. Delete a wrong DOI in Zotero and the archive keeps the
stale one, because "no DOI" and "leave the DOI alone" become the same message.
That is already the price paid for `note` and `item_type`.

`url_or_path` is assigned unconditionally, matching what it does today: it is
recomputed from the ladder on every write, and a stale value is exactly the
problem this change exists to fix.

Note the embed path is not a candidate — `_load_zotero_items_for_embed` loads
only items still needing chunks, so it sees a shrinking subset.

## The write-path signature

`insert_document_if_new` and `upsert_document` are **raw SQL with explicit column
lists**, not Core inserts, and both already take nine parameters. Seven more makes
sixteen, in two functions whose INSERT, VALUES and `ON CONFLICT DO UPDATE`
clauses must each be edited in step.

Most of the eleven call sites go through per-runner `_document_kwargs()` helpers
(Zotero, Reddit, YouTube), so they absorb new fields for free; Calibre, Firefox
and the image pipeline pass arguments directly. Worth deciding up front whether
this is the change that turns those parameters into a dataclass or a `**fields`
mapping. **Recommendation: not in this change** — do the plumbing plainly, and
let a fourteen-parameter signature be the argument for a follow-up that touches
only the two functions and their call sites, rather than mixing a refactor into a
migration. New columns follow the existing `COALESCE(excluded.x, documents.x)`
idiom in the update clause, matching `item_type` / `note` / attachment key.

## Testing

- Migration: an archive created without the columns gains them on `init_db()`,
  twice in a row, with existing rows preserved — the shape `test_schema_migration.py`
  already uses. Assert all three identifier indexes exist.
- Canonical forms land in the columns, not raw source strings: a hyphenated ISBN
  is stored normalised, a failed checksum is stored as `NULL` rather than as a
  bad join key, and `2301.12345v3` collapses to `2301.12345`.
- **The derived arXiv DOI**, in both directions: an arXiv document with no source
  DOI gets `10.48550/arxiv.<id>`, and one that *does* carry a source DOI keeps it
  untouched. The second assertion is the one that matters — a journal DOI
  overwritten by the derived form is a silent downgrade.
- **The cross-source join this exists for:** a Zotero item whose `url` is an
  arXiv abs link and a Firefox document fetched from the same arXiv URL end up
  with equal `arxiv_id` *and* equal `doi`. A Zotero item with a non-arXiv URL
  leaves `arxiv_id` NULL rather than guessing.
- `zotero_url` / `zotero_path` are populated **independently**: an item with both
  a URL and an attachment keeps both, which is the regression the split exists to
  prevent. Plus the ladder: `url_or_path` still prefers the URL, falls back to the
  path, and no longer falls back to the DOI.
- Non-http `url` values reach `zotero_url` rather than being dropped.
- `authors_json` round-trips a list, preserves order, and is a JSON array for a
  single author. bioRxiv's `str` authors normalise to a list.
- `refresh_zotero_metadata` updates an already-archived row (the
  `test_zotero_links.py` backfill test is the template) and does not null out a
  stored DOI when the incoming item has none.
- **The DOI-only migration**, which is the regression risk of the whole change:
  seed a row whose `url_or_path` holds a bare DOI string (an archive written by
  the old ladder), run the backfill, and assert `url_or_path` is now `NULL`,
  `doi` holds the identifier, and a newly inserted DOI-only item agrees with it.
  Old and new rows converging is the assertion — a test that only checks the new
  insert path would pass while the split-brain ships.
- Population: a Zotero item with DOI/year/authors lands all three; a Calibre book
  lands isbn, year and authors.

## Doc sync

`docs/ingestion-flows.md` **needs no redraw** — stated explicitly because
`CLAUDE.md` makes it a same-commit rule and the next reader will check. This adds
no source, changes no phase shape, moves nothing across the shared/source-specific
line, adds no outbound call, and does not touch the shared tail. It persists
fields the connectors already parse.

`DESIGN.md` does need editing: §3.2's Zotero row currently says the real gap is
un-ingested PDF attachments, which stays true but is no longer the *only* gap
worth naming there. Add a short note on where structured metadata lives and why
it is on `documents` rather than in a sidecar — the sidecar question will
otherwise be re-asked every time someone reads `reddit_items`.

## Phasing

1. Schema + migration + index. Nothing reads the columns yet.
2. Zotero: connector split, `_zotero_document_kwargs`, `refresh_zotero_metadata`,
   and the `url_or_path` ladder change together with the API's DOI link.
3. Calibre: isbn + year + authors, all three already parsed and discarded.
4. API + frontend surfacing: `DocumentOut` / `DocumentDetail` gain the fields;
   the detail panel can finally show authors.
5. Preprints, from the fetch path: bioRxiv DOI, arXiv ID plus its derived DOI,
   authors on both.

The dedup pass is downstream of step 2 and out of scope here.

## Accepted costs

- **Three `zotero_*` columns on `documents`.** `doi` / `year` / `authors_json`
  are genuinely cross-source; `zotero_url` / `zotero_path` are not, and with no
  sidecar they sit in the shared table beside `zotero_attachment_key`. Recorded
  as a decision, not an open question.
- **A cache, not a rescue.** Unlike `reddit_items` — justified in `DESIGN.md` by
  Reddit being the one source with no local original — Zotero's SQLite is local
  and authoritative, so these columns are a queryable copy that goes stale
  between syncs. The same is already true of source tags and collections.

## Settled, so they are not re-litigated

- **ISBN and arXiv ID get their own columns**, alongside `doi`. Both are already
  parsed and discarded today, both are dedup keys, and both have a canonicaliser
  in the tree already.
- **`doi` is derived from `arxiv_id`** when the source supplies no DOI, so
  preprints are not a hole in every DOI-keyed path. A source DOI always wins.
- **Zotero populates `arxiv_id` from its `url`** via the existing
  `parse_arxiv_url`. Partial coverage (`Extra` is not read) is accepted — it is
  the join that makes a Zotero preprint and a fetched arXiv bookmark the same
  paper, and free.
- **`year` is never derived from `date_added`.** When you saved something says
  nothing about when it was published, and a confidently wrong year is worse than
  a null one — including for the browse filters, which mean `date_added` anyway.
- **Images are untouched.** No identifier of their own, and cover extraction is a
  different mechanism with a multi-book shape that `images.books_json` already
  holds.

## Open questions

- Should the Zotero connector load `Extra` so arXiv IDs recorded there (Zotero's
  own translator writes `arXiv:2301.12345` into it) are caught too? It is one
  more entry in `_FIELD_NAMES` and a regex, but it widens what the connector
  reads, so it is a decision rather than a detail. Until then, `arxiv_id`
  coverage on Zotero rows is partial.
