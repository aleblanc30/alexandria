# Tag deduplication

Collapse the variants of one tag — `Machine Learning`, `machine learning`,
`machine-learning`, `machinelearning` — into a single browsable, filterable,
countable tag, without rewriting what a source actually said.

Covers the `TODO.md` item *"Deduplication of tags"* under *Ingestion &
deduplication*. The sibling item *"Deduplication of items"* is a different
problem (document identity) and is planned in `ITEM_DEDUPLICATION.md`; the two
share a shape — a derived relation consulted at read time rather than a
destructive rewrite — but no code.

Not authoritative about current behavior — this is proposed work.

---

## 0. Where the duplicates come from

There are two tag namespaces, and only one of them is normalised.

| Origin | Written by | Normalised? |
|--------|-----------|-------------|
| `source` | `insert_source_tags` (`db/queries.py:402`), called by Firefox (`runners/firefox.py:66`), Calibre (`runners/calibre.py:131,180`), YouTube (`runners/youtube.py:52`), Zotero (`runners/zotero.py:91,138`) | **no** — the source string is stored verbatim |
| `inferred` (classification) | `sync_classification_tags` (`classification.py:91`) | closed vocabulary of four (`CLASSIFICATION_TAGS`) |
| `inferred` (image type) | `image_pipeline.py:437` | closed vocabulary (`_VALID_TYPES`) |
| `manual` | `patch_tags` (`api/routers/documents.py:108`) | **no** — see §2 |
| `llm` | `apply_tag_to_documents` (`clustering/cluster_tags.py:87`) | yes, `slugify_tag` |
| `cluster_l1` / `cluster_l2` | `label_to_tag` → `slugify_tag` | yes |
| `learned` | `tag_training/lifecycle.py:162,432` | yes |

So the duplicate mass sits in `source_tags`, where four connectors write free
text straight through. `slugify_tag` (`clustering/cluster_tags.py:18`) already
exists and does exactly the right thing — lowercase, strip punctuation, collapse
whitespace to hyphens, truncate at 64 — but no source-tag path calls it.

YouTube is the worst case by construction: `video.tags` are creator-supplied
keywords, dozens per video, with no vocabulary discipline at all. Zotero and
Calibre tags are user-curated and mostly consistent within one library, but not
*across* libraries — a Zotero `Machine Learning` and a Calibre `machine
learning` are two rows today.

The read sites that suffer are the tag index (`list_tags`, `db/queries.py:1212`),
the browse filters (`_where_source_tag` at `:981`, `_where_overlay_tag` at
`:991`), the card chips (`_browse_tag_maps` at `:1090`), cluster labelling
(`top_tags_for_cluster`, `cluster_tags.py:98`), and the training seed
(`document_ids_for_source_tag`, `tag_training/lifecycle.py:56`). That last one
is the sharpest: seeding a classifier from the source tag `Machine Learning`
silently excludes every document that spelled it `machine learning`, so the
positive set is smaller than the user believes it to be.

---

## 1. Four kinds of duplicate, two kinds of decision

| Class | Example | Mechanical? |
|-------|---------|-------------|
| case, spacing, punctuation | `Machine Learning` / `machine-learning` / `MACHINE LEARNING` | yes — one function, no judgement |
| morphology | `neural-network` / `neural-networks` | rule-derivable, but wrong often enough to need review (`physics` is not `physic`, `glasses` is not `glass`) |
| abbreviation | `ml` / `machine-learning`, `nn` / `neural-networks` | no |
| synonym | `deep-learning` / `neural-networks` | no, and frequently not even correct |

The first class is a normalisation. The other three are decisions someone has to
make and the archive has to remember. That split is the whole structure of this
plan: §3 derives the first automatically, §4 persists the rest.

**Cross-origin collapse is out of scope.** A source tag `neural-networks`, a
cluster label `neural-networks` and a learned tag `neural-networks` stay three
separate entries. Origin is load-bearing — the browse filters are split by it
(`general_tags`, `cluster_l1_tags`, `cluster_l2_tags`, `learned_tags` are
separate parameters), and a cluster label means "this run grouped these
documents", which is not the same claim as "the user filed it here". Folding
them would make the tag index tidier and the filters wrong. The tag index may
*display* them adjacently; it must not sum them.

---

## 2. The one place the archive makes its own duplicates

`patch_tags` (`api/routers/documents.py:108`) calls `insert_overlay_tags`
directly, with no `slugify_tag`, while every other overlay writer goes through
a slug. Type `Machine Learning` into the detail panel's tag box
(`DocDetailPanel.vue:211`, which only `.trim()`s) and the archive stores a
manual tag that will never match the cluster tag `machine-learning` it was meant
to reinforce.

Fix this first, independently of everything below: slugify in `patch_tags` on
both the `add` and `remove` sides, so removal keeps matching what insertion
wrote. Then a one-off backfill collapses existing manual tags to their slug,
which needs care in exactly one place — `overlay_tags` is unique on
`(document_id, tag, origin)` (`schema.py:153`), so a document carrying both
`Machine Learning` and `machine-learning` as manual tags must have the loser
deleted rather than updated into a constraint violation.

This is a handful of lines and it is the only *new* duplicate generation inside
Alexandria's own control. Everything else in this plan is about duplicates the
sources hand us.

---

## 3. Layer one: a derived key on `source_tags`

Add one column, `source_tags.tag_key`, populated by `slugify_tag(tag_string)` at
insert time and backfillable at any moment. Indexed on `(tag_key)` and, for the
browse EXISTS, on `(document_id, tag_key)` alongside the existing
`ix_source_tags_document_id_tag_string`.

- `tag_string` is untouched and remains the record of what the source said, the
  same rule `archive/COLLECTION_TAGS.md` §1 states for `source_collections` and
  `ITEM_DEDUPLICATION.md` §1 states for `documents.isbn`.
- Because the key is derived, changing the normalisation rule is a backfill
  (`alexandria dedupe-tags rekey`), never a resync. This is the reason to store
  a derived column rather than normalise on the way in and lose the original.
- `overlay_tags` needs **no** such column. Once §2 lands, every overlay writer
  slugifies, so `tag` already *is* the key. One column on one table.

Display form: the tag index shows the most frequent `tag_string` behind a key,
not the key itself, so a library that consistently writes `NASA` keeps seeing
`NASA` rather than `nasa`. Ties break on the first alphabetically, so the
display form is stable across runs.

Migration: `ALTER TABLE source_tags ADD COLUMN tag_key TEXT` in `init_db`'s
migration block, plus the two `CREATE INDEX IF NOT EXISTS` statements, following
the P-1 pattern. The backfill runs on demand rather than inside `init_db` — an
`alexandria init` against a large archive should not silently rewrite every tag
row — and every read site treats a NULL `tag_key` as "fall back to
`tag_string`", so a half-migrated archive degrades to today's behavior instead
of losing tags.

---

## 4. Layer two: `tag_aliases`

```python
tag_aliases = sa.Table(
    "tag_aliases", meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("alias", sa.Text, nullable=False),        # normalised key, the losing form
    sa.Column("canonical", sa.Text, nullable=False),    # normalised key, the surviving form
    sa.Column("kind", sa.Text, nullable=False),         # morphology|abbreviation|synonym|manual
    sa.Column("state", sa.Text, nullable=False),        # candidate|active|rejected
    sa.Column("decided_by", sa.Text),                   # auto|user|llm
    sa.Column("decided_at", sa.Integer),
    sa.UniqueConstraint("alias", "canonical", name="uq_tag_alias_pair"),
    sa.Index("ix_tag_aliases_canonical", "canonical"),
    sa.Index("ix_tag_aliases_state", "state"),
    sa.Index("uq_tag_aliases_active", "alias", unique=True, sqlite_where=sa.text("state = 'active'")),
)
```

Both sides hold *normalised keys*, so layer one has already run and the alias
table never has to think about case. Four properties it relies on:

- **The partial unique index is the no-two-canonicals invariant**, exactly as in
  `ITEM_DEDUPLICATION.md` §4: a form may be a candidate against several
  canonicals but `active` under one.
- **No chains.** The writer refuses an alias whose `canonical` is itself an
  active alias, rewriting it to that alias's canonical. Read sites then expand
  one hop.
- **`rejected` is permanent**, so a re-scan does not re-propose a pair the user
  already declined.
- **Aliases are document-independent**, so purging documents never invalidates
  one. An alias for a tag that currently has no rows is inert and becomes useful
  again if the tag returns on the next sync. Nothing prunes them automatically.

One validation rule: an `alias` may not be a member of `CLASSIFICATION_TAGS` or
the image `_VALID_TYPES`. Those vocabularies are closed and their writers
re-insert them on every run, so aliasing `video` away would leave the index
folding a tag the writer keeps recreating. Naming one as the *canonical* side is
fine.

---

## 5. What the read path does

One helper module, `pka/tags/aliases.py`:

```
tag_key(raw) -> str                       # slugify_tag, re-exported as the one entry point
alias_map(con) -> dict[str, str]          # active aliases only, small enough to cache per request
expand(con, tag) -> list[str]             # canonical -> every key that folds into it
fold(con, tags) -> list[str]              # keys -> canonical, order preserved, deduplicated
```

| Site | File | Change |
|------|------|--------|
| Tag index | `db/queries.py:1212` (`list_tags`) | group by `COALESCE(tag_key, tag_string)` folded through `alias_map`, sum per canonical, pick the display form per §3 |
| Source-tag filter | `db/queries.py:981` | `tag_string == tag` becomes `tag_key IN expand(tag)`; the existing EXISTS shape is unchanged |
| Overlay-tag filter | `db/queries.py:991` | same expansion, still scoped by `origin` |
| Card chips | `db/queries.py:1090` | fold each document's tags to canonical and deduplicate, so a card carrying two variants shows one chip |
| Cluster labelling input | `clustering/cluster_tags.py:98` | group by folded key, so `top_tags_for_cluster` stops splitting one concept's votes across spellings |
| Training seed | `tag_training/lifecycle.py:56` | expand before the `IN`, so the positive seed picks up every spelling |

Deliberately unchanged: the writers. Sources keep writing verbatim strings,
`sync_classification_tags` keeps its closed vocabulary, and nothing about a sync
consults the alias table. A tag decision is a view over the archive, not a step
in ingestion, which is why `docs/ingestion-flows.md` needs no redraw (§9).

---

## 6. The count is wrong before folding makes it worse

`list_tags` counts rows: `sa.func.count(source_tags.c.id)` grouped by
`tag_string` (`db/queries.py:1257`). `TagView.vue` renders that under a column
headed **Docs**. A document tagged `python` in both Zotero and Firefox is two
rows, so it already counts twice.

Folding multiplies the error — every variant a single document carries adds
another row to the same canonical — so the fold must switch both branches to
`COUNT(DISTINCT document_id)`. Worth doing even if the rest of this plan is
never built: it is a one-word change that makes an existing number mean what its
column header says.

---

## 7. Proposing aliases

`pka/tags/scan.py`, producing candidates for §4. No writes to `overlay_tags` or
`source_tags` at any point.

- **Case and punctuation**: not proposed at all. Layer one folds them with no
  decision to record, which is the point of splitting the layers.
- **Morphology**: hand-written suffix rules over the key set — `-s`, `-es`,
  `-ies`/`-y`, and a stop list for the traps (`physics`, `mathematics`,
  `glasses`, `series`). Proposed as `candidate`, never auto-activated. No new
  dependency: a real lemmatizer means a spaCy model, and `_get_spacy`
  (`ingestion/chunker.py:63`) deliberately falls back to `spacy.blank("en")`
  precisely so that no model download is ever needed — adding one would put a
  network fetch behind a tag report, against `DESIGN.md` §1.1.
- **Abbreviations**: proposed only for the initialism case, where every letter
  of the short form starts a word of the long form (`ml` ↔ `machine-learning`)
  **and** both already exist in the archive. This is a narrow rule that produces
  few candidates and almost no nonsense.
- **Synonyms**: not derivable. Optional local-LLM proposal pass, sending the top
  N tag keys and asking for grouping, behind its own default-off setting
  (`tag_alias_llm_enabled`), never escalating from `bookmark_summary_enabled` or
  any other flag, per `DESIGN.md` §1.1. Output is `candidate` rows with
  `decided_by='llm'`, which a user still has to accept. Note for whoever builds
  it: tag names are among the more revealing things in the archive, since they
  are the user's own vocabulary, so a hosted provider configuration sends the
  shape of someone's research interests to a third party. Default off is the
  minimum; a warning next to the setting is better.

---

## 8. Surfaces

```
alexandria dedupe-tags report [--json]        # variant groups layer one would fold, before rekey
alexandria dedupe-tags rekey [--dry-run]      # (re)populate source_tags.tag_key
alexandria dedupe-tags scan [--kind morphology,abbreviation] [--llm]
alexandria dedupe-tags list [--state candidate]
alexandria dedupe-tags merge <alias> <canonical>
alexandria dedupe-tags reject <alias> <canonical>
alexandria dedupe-tags unmerge <alias>
```

Registered in `cli/__init__.py`'s `COMMANDS` next to the purge family.

`TagView.vue` is the natural UI: it already renders one row per `(tag, origin)`
with an action column (today only "Train classifier…" on source rows). A
"Merge…" action opening a small picker of similar tags fits without restructuring
the view. Two smaller wins land with it — the **Sources** column, currently a
hardcoded `—`, can show which connectors contribute a canonical tag, and the
merged row can list its variants on hover, so a fold is visible rather than
mysterious.

API: `GET /tags/aliases`, `POST /tags/aliases`, `DELETE /tags/aliases/{alias}`,
and a `GET /tags/candidates`. `/tags` itself keeps its shape; its rows just
become canonical rows.

---

## 9. Phases

**Phase 0 — the manual-tag slug fix (§2)** plus the `COUNT(DISTINCT)` correction
(§6). Independent of everything else, small, and it stops the archive adding to
the problem while the rest is built.

**Phase 1 — layer one.** `tag_key`, its indexes, the backfill command, and the
`report` output. The report is what says whether layers two and three are worth
building: if a real archive has thirty variant groups, the CLI is enough and the
UI is not needed.

**Phase 2 — the read path (§5).** This is where the user sees a shorter tag list
and filters that stop missing documents.

**Phase 3 — `tag_aliases` and the scan**, CLI only.

**Phase 4 — the TagView merge UI**, if Phase 1's numbers justify it.

The LLM synonym pass is a separate decision at the end, not a phase — it is the
only part with a `DESIGN.md` §1.1 flag and the only part that can be wrong in an
interesting way.

---

## 10. Open questions

- **Should a fold rewrite `source_tags` after all?** Keeping `tag_string`
  verbatim costs an index and a join-ish expansion on every tag filter. If
  Phase 1's report shows the variant count is small and the display-form rule
  never surprises anyone, a future compaction could collapse the rows outright.
  Leaning against: the current design lets the normalisation rule change without
  a resync, and that has already proved useful for `source_collections`.
- **Per-source alias scope.** All aliases are global today. A Calibre `fiction`
  and a Zotero `fiction` are probably the same idea, but a YouTube creator's
  keyword spam may deserve folding rules that a curated Zotero library should
  not inherit. Deferred until the report shows whether the two libraries even
  overlap.
- **Whether cluster labels should be folded against source tags for
  *labelling*** — not for filtering (§1 rules that out), but
  `top_tags_for_cluster` feeds the LLM that names a cluster, and there the
  variants are pure noise. §5 folds within source tags only; folding a cluster's
  input against overlay vocabulary is a separate, smaller question.

---

## 11. Tests and docs

New: `tests/test_tag_keys.py` (the normalisation table, including `NASA`, `C++`,
a 70-character tag, and a tag that slugs to empty), `tests/test_tag_aliases.py`
(chain flattening, the partial-unique invariant, the closed-vocabulary guard,
rejected pairs staying skipped), `tests/test_tag_scan.py` (morphology rules and
their stop list, the initialism rule, no candidates for pure case variants).
Extend `tests/test_db.py` for folded `list_tags` counts and expanded filters,
`tests/test_api.py` for the slugified `patch_tags` round trip, and
`tests/test_schema_migration.py` for the new column, its indexes and the new
table.

`docs/persisted-fields.md` needs `source_tags.tag_key` in its side-table section
and `tag_aliases` noted as written by no source, the same maintenance-artifact
category as `cluster_runs`. `docs/ingestion-flows.md` needs **no change**:
nothing here sits in a pipeline phase, moves across the shared/source-specific
line, or adds an outbound call — the one optional outbound path, the LLM synonym
pass, is a report over the tag index rather than an ingestion step. `DESIGN.md`
gains a short subsection describing the two layers and the six read sites that
honour them, next to the existing tag-origin material.
