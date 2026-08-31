# Collapsing the `documents` write-path signature

**Status:** implemented.
**Follows:** `planning/DOCUMENT_METADATA_PLAN.md` → *The write-path signature*,
which deferred exactly this refactor so a migration would not carry one.
**Unblocks:** `planning/TODO.md` → *Deduplication of items*, which needs a
mergeable document value and not just a joinable DOI column — see *Decision*.
**Touches:** `pka/db/queries.py` (the refactor), `pka/ingestion/runners/zotero.py`,
`reddit.py`, `youtube.py`, `calibre.py`, `pka/ingestion/runners/firefox.py`,
`pka/ingestion/image_pipeline.py` (call sites), `tests/conftest.py` (new factory)
and ~20 test modules. `pka/db/schema.py` is read, not edited.

**Implementation notes** (where the shipped code diverges from the sketches
below — the design intent they illustrate is unchanged):

- `make_document`'s real signature mirrors the pre-refactor *positional* shape
  of `upsert_document` (`source, source_id, title=None, url_or_path=None,
  date_added=None, fetch_status="pending", zotero_attachment_key=None,
  item_type=None, note=None, **fields`), not the bare `**fields` sketched under
  *Testing*. That turned the ~80-site conversion into a pure rename at every
  call site that only used positional/keyword args already shaped that way —
  safer than re-deriving each call into keyword form, and the illustrative
  "no ceremony `None`" call shown there was just that: illustrative.
- `insert_document_if_new` call sites outside `test_db.py` (progress-tracker
  and progress-baselines tests, ~8 sites) were **not** routed through
  `make_document` — that factory only ever calls `upsert_document`, and
  rewriting an insert-if-new call through it would silently change its
  duplicate-detection contract. Those sites construct `DocumentWrite` directly
  instead, same as `test_db.py`.
- Open question 1 (recovering the id): resolved by measurement, not
  assumption — `inserted_primary_key` was verified to return the *wrong* row's
  id on a real conflict (SQLAlchemy 2.0.50 + pysqlite), confirming the trailing
  `SELECT`-by-key was the only safe option in both `on_conflict_do_update` and
  `on_conflict_do_nothing`. The leading existence-check `SELECT` was dropped;
  the trailing one was not.
- Open question 2 (`rowcount == 0` on `DO NOTHING`): confirmed reliable by the
  same measurement.
- Open question 4 (`merge()`): not shipped, per the plan's own recommendation —
  no caller exists yet.
- The one-off AST-based rewrite script used to convert call sites
  (`scripts/_wrap_docwrite.py`) was deleted after use; it was not part of the
  shipped change.

**This is deliberately not a minimal diff.** `CLAUDE.md` asks for minimal diffs
and this plan overrides that, with reasons stated under *Decision*. That
override is the main thing to agree or disagree with before any code is written.

## The problem

`insert_document_if_new` and `upsert_document` (`pka/db/queries.py:184` and
`:253`) are raw `sa.text()` SQL with hand-maintained, hand-parallel column
lists. Each function names every writable column three times over:

| Place | `insert_document_if_new` | `upsert_document` |
|---|---|---|
| Python signature | 16 params | 16 params |
| `INSERT INTO documents (…)` | 17 names | 17 names |
| `VALUES (…)` | 17 placeholders | 17 placeholders |
| params dict | 17 keys | 17 keys |
| `ON CONFLICT DO UPDATE SET` | — | 12 assignments |

Adding one column today means editing **seven lists across the two functions**,
plus `schema.py`, plus the `init_db()` migration, plus
`_ZOTERO_REFRESH_COALESCE_KEYS` (`:337`) when Zotero populates it. The
`DOCUMENT_METADATA_PLAN.md` pass did exactly that and it is why this entry
exists.

The count is not the real cost. The real cost is that the lists must stay
**positionally parallel** — `:zurl` in `VALUES` is the fourteenth placeholder
and must line up with `zotero_url` as the fourteenth column name, and nothing
checks it. A transposition between two adjacent `TEXT` columns writes the wrong
value into the right column, silently, with no error and no failing test unless
a test happens to assert on both. The `VALUES` placeholders are also abbreviated
(`:zak`, `:da`, `:fs`, `:zurl`, `:zpath`) where the column names are not, so the
reader cannot check alignment by eye.

Two smaller defects ride along:

- `insert_document_if_new` does `SELECT` → `INSERT` → `SELECT` — three
  statements and a check-then-act race, where SQLite has had
  `ON CONFLICT DO NOTHING` since 3.24 (env has 3.42, SQLAlchemy 2.0.50).
- The `DO UPDATE` clause hard-codes which columns overwrite and which
  `COALESCE`. A new column is silently *omitted* from that clause if forgotten,
  meaning it is written on insert and then never updated — the failure mode is
  a stale value, not an exception.

## Decision

A frozen **`DocumentWrite` dataclass is the sole parameter** of both write
functions. It is the single declaration of the writable column set; the SQL is
derived from it by one internal Core writer.

```python
def insert_document_if_new(doc: DocumentWrite) -> int | None: ...
def upsert_document(doc: DocumentWrite) -> int: ...
```

No positional prefix, no `**fields`, no compatibility wrapper. Every call site
in `pka/` and `tests/` is rewritten.

### Why dataclass-only, and not a wrapper that preserves the call sites

An earlier draft of this plan kept the six hot positional parameters and passed
the ten metadata columns through `**fields`, to avoid touching 116 test call
sites. That was the wrong call, for three reasons.

**1. Dedup needs a value, not a keyword bag.** *Deduplication of items* is the
feature this entire metadata thread exists to unblock, and it is not merely a
query — it is a merge. Two rows for the same paper from Zotero and bioRxiv have
to be reconciled field by field: take the DOI from whichever has one, keep the
earlier `date_added`, union the authors. That is `dataclasses.replace()`, field
iteration, and a `merge(a, b)` function with an obvious home. A `**fields` dict
gives none of that structure, so the wrapper design would have to grow a value
type anyway, six months later, next to the wrapper it was meant to avoid.

**2. The positional prefix had no justification left.** Its only purpose was
keeping `upsert_document("zotero", "K1", "T", None, None)` compiling in tests.
That is production API shape dictated by test convenience. Once the tests are
in scope, what remains is a half-measure: a function with six explicit
parameters and an untyped hole behind them, which is worse than either end of
the trade.

**3. The runner helpers become correct instead of tolerated.** Zotero, Reddit
and YouTube each build a `dict(...)` in a `_document_kwargs()` helper and splat
it. Under the wrapper design those stayed dicts and the earlier draft called
converting them "a pointless diff". They are in fact the exact places the
metadata columns get populated, and typing them is the point:
`_zotero_document_kwargs(item) -> DocumentWrite` turns the module that knows the
most about the columns into the module the type system checks hardest.

### The cost, stated plainly

116 test call sites and 11 in `pka/`. That is the price. What makes it worth
paying rather than merely survivable is that most of it does not become
`DocumentWrite(...)` at all — see *Testing*, where ~80 of those sites get
**shorter** than they are today.

### The three pieces

**1. `DocumentWrite`** — the one place the writable columns are listed.

```python
@dataclass(frozen=True, slots=True)
class DocumentWrite:
    """The columns of ``documents`` that ingestion writes.

    Not every column: ``archive_url``, ``card_summary``, ``generated_summary``
    and ``doc_embedding`` are owned by their own helpers (``update_card_summary``,
    ``set_generated_summary``, the Wayback and doc-embedding paths) and are never
    written by an ingestion upsert. ``ingested_at`` is set by the writer.
    """

    source: Source | str
    source_id: str
    title: str | None = None
    url_or_path: str | None = None
    date_added: int | None = None
    fetch_status: FetchStatus | str = FetchStatus.PENDING
    zotero_attachment_key: str | None = None
    item_type: str | None = None
    note: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    isbn: str | None = None
    year: int | None = None
    authors_json: str | None = None
    zotero_url: str | None = None
    zotero_path: str | None = None

    def values(self) -> dict[str, Any]:
        """Column → value, with the two string enums stringified."""
        v = asdict(self)
        v["source"] = str(self.source)
        v["fetch_status"] = str(self.fetch_status)
        return v
```

Field order matches today's parameter order, so a positional
`DocumentWrite("zotero", "K1", "T", None, None)` still means what the old call
meant — useful while converting, not something to rely on afterwards.

**2. The update policy** — two names, not twelve assignments.

```python
# An upsert overwrites these; every other column COALESCEs the incoming value
# over the stored one. COALESCE is the right default for a bibliographic field:
# a source that does not know an item's DOI must not erase the DOI another
# source supplied. Overwrite is reserved for the three columns whose current
# value is by definition whatever the source last said.
_OVERWRITE_ON_UPSERT = frozenset({"title", "url_or_path", "fetch_status"})
```

`ingested_at` is neither: it is `COALESCE(documents.ingested_at, excluded.ingested_at)`
— the stored value wins — so it stays a named special case in the writer, with
the comment currently on `upsert_document` moved to it.

This is the part that makes a new column free. Add `publisher` to the dataclass
and it is inserted, and it is COALESCEd on conflict, with no third edit and no
chance of being forgotten out of an update clause.

**3. `_write_document`** — one Core statement, both modes.

```python
def _write_document(doc: DocumentWrite, *, on_conflict: str) -> int | None:
    values = doc.values() | {"ingested_at": int(time.time())}
    stmt = sqlite_insert(documents).values(**values)
    if on_conflict == "update":
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "source_id"],
            set_={
                col: (
                    stmt.excluded[col]
                    if col in _OVERWRITE_ON_UPSERT
                    else sa.func.coalesce(stmt.excluded[col], documents.c[col])
                )
                for col in doc.values()
                if col not in ("source", "source_id")
            }
            | {"ingested_at": sa.func.coalesce(documents.c.ingested_at, stmt.excluded.ingested_at)},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["source", "source_id"])
    ...
```

**The two public functions stay two functions**, not one with a mode flag: they
have genuinely different return contracts (`int | None` vs `int`) and a union
return behind a string flag would be a worse API than the pair. `on_conflict`
stays private to the writer.

## What a new column costs, before and after

| | Today | After |
|---|---|---|
| `schema.py` column | 1 | 1 |
| `init_db()` migration | 1 | 1 |
| Writable-column declaration | 2 (both signatures) | 1 (`DocumentWrite`) |
| `INSERT` / `VALUES` / params | 6 | 0 |
| `DO UPDATE SET` | 1 | 0 |
| Parallel lists that can silently transpose | 4 | 0 |

## Phasing

Each phase is independently committable and leaves `pytest` green.

1. **Characterisation tests**, written against the *current* functions and
   committed before anything changes, so they are provably not written to fit
   the new implementation. See *Testing*.
2. **`DocumentWrite` + `_write_document` + the new signatures**, and the 11
   call sites in `pka/` in the same commit — they cannot compile apart. The
   three `_document_kwargs()` helpers change return type from `dict` to
   `DocumentWrite` here; Calibre, Firefox and the image pipeline gain a
   `DocumentWrite(...)` at the call.
3. **`tests/conftest.py` factory + the test conversion.** Mechanical, and
   separable from phase 2 only if phase 2 ships with the tests already
   converted — so in practice 2 and 3 land together and phase 3 is a review
   convenience, not a commit boundary.
4. **Optional, decide separately: `refresh_zotero_metadata`.** It is the fourth
   place a column name is listed (`_ZOTERO_REFRESH_COALESCE_KEYS`) and it
   already uses the same `COALESCE(incoming, stored)` shape, so sharing the
   policy is tempting. But it deliberately updates a **subset** — it never
   touches `title`, `item_type`, `note`, `isbn` or `fetch_status`, because a
   post-hoc Zotero refresh must not reset a fetch status — and it writes
   `url_or_path` unconditionally. Sharing would mean an `only=` restriction
   threaded through the policy, which is arguably worse than the seven-name
   tuple it replaces. **Recommendation: leave it alone**, and add a comment
   pointing at `DocumentWrite` so the next column author sees the second site.

## Testing

### The conversion is not 116 constructor wraps

Only **13** of the 116 test call sites pass any metadata column. The other ~103
are fixture noise — `upsert_document("firefox", "F1", "T1", "https://ok.com/page", None)`
creating a throwaway row to hang an unrelated test on, with a trailing `None`
for a `date_added` nobody cares about. Wrapping those in `DocumentWrite(...)`
would make them longer for no gain, which is what made the churn look like pure
cost in the first draft.

Split them instead:

- **~80 sites outside `tests/test_db.py`** move to a new `tests/conftest.py`
  factory, matching the `_make_zotero_db` / `_make_reddit_saved_items` helpers
  already there:

  ```python
  def make_document(source, source_id, **fields) -> int:
      """Insert a throwaway document and return its id."""
      return upsert_document(DocumentWrite(source, source_id, **fields))
  ```

  `upsert_document("firefox", "F1", "T1", "https://ok.com/page", None)` becomes
  `make_document("firefox", "F1", title="T1", url_or_path="https://ok.com/page")`
  — keyword-named, no ceremony `None`, and no longer coupled to the write-path
  signature at all. **This is the part that makes the churn pay for itself**: a
  future signature change stops reaching 20 test modules, because only the
  factory and `test_db.py` touch the real API.
- **36 sites in `tests/test_db.py`** construct `DocumentWrite` explicitly. That
  module *is* the write-path test; it should exercise the real surface, not the
  convenience factory.
- **13 metadata-carrying sites** likewise explicit, wherever they live.

### The tests themselves

Write these against the existing implementation first (phase 1), watch them
pass, then refactor and watch them pass unchanged.

- **Every column round-trips.** One write with a distinct sentinel value in all
  sixteen fields, then read the row back and assert field-by-field. This is the
  transposition test the current code has no equivalent of, and it is the reason
  to write it before touching anything. Same for `insert_document_if_new`.
- **Overwrite vs COALESCE, per column.** Write a full row, write again with
  `None` in every optional column, and assert: `title`/`url_or_path`/
  `fetch_status` took the second (overwriting) value, everything else kept the
  first. Then write a third time with new non-`None` values and assert they all
  land. `test_db.py:88` covers the title case only.
- **`ingested_at` survives.** Assert it explicitly — its COALESCE runs the
  opposite direction from every other column and is the one most likely to be
  broken by a policy-driven rewrite.
- **`insert_document_if_new` contract.** Returns the new id on first insert,
  `None` on a duplicate, and — new — leaves the existing row *completely*
  unmodified on the duplicate path, including columns the caller passed a value
  for. `test_db.py:111` asserts the `None` but not the non-mutation.
- **Enum and string forms agree.** `Source.ZOTERO` and `"zotero"` write the same
  `source` value; likewise `FetchStatus.PENDING` and `"pending"`. Today's
  `str(source)` is doing that work and the Core path must keep it.
- **`DocumentWrite` covers exactly the intended columns.** Assert its field
  names against `documents.c` minus the four helper-owned columns and
  `id`/`ingested_at`. This is what keeps the dataclass from drifting out of sync
  with the table, and it is the test that replaces four hand-maintained lists.

Run `pytest` (not `--cov`) while iterating; `--cov` enforces `fail_under = 85`
and the two functions are heavily exercised, so coverage should rise, not dip.

## Accepted costs

- **A large, mostly mechanical diff**, overriding `CLAUDE.md`'s minimal-diff
  rule. Reviewable because it separates cleanly: one file of real design change,
  one factory, and a long tail of call-site rewrites that either shrink or stay
  the same length.
- **`sqlite_insert` pins the dialect.** `sqlalchemy.dialects.sqlite.insert` is
  already the only sensible choice — `on_conflict_do_update` is dialect-specific
  and the raw SQL being replaced was already SQLite-only (`ON CONFLICT`,
  `excluded.`). No portability is lost that existed.
- **The generated SQL is no longer greppable.** Someone debugging can no longer
  read the statement in the source. Mitigation: `echo=True` on the engine, or
  `print(stmt.compile(eng))`; worth a one-line comment in the writer saying so.
- **`slots=True` on a frozen dataclass** means no `__dict__` and no late
  attribute assignment. That is wanted here — it makes a typo an `AttributeError`
  — but it is a behaviour change if anything ever monkeypatches an instance.
  Nothing does.
- **Every call site now imports `DocumentWrite` alongside the function.** Minor,
  but it is 25-odd import lines that did not exist.

## Settled, so they are not re-litigated

- **The dataclass lives in `pka/db/queries.py`**, under the existing
  `# ── Documents ──` banner, not in a new `pka/db/documents.py`. The project's
  stated layout is "queries in `pka/db/queries.py`" (`.claude/rules/python.md`).
  The file is 1275 lines and splitting it may eventually be right, but that is a
  separate argument and this change should not be the one that starts it.
- **Two functions, not one with a mode flag.** Different return contracts.
- **`documents` stays a Core `sa.Table`, no ORM model.** Explicit in
  `.claude/rules/python.md`. `DocumentWrite` is a plain dataclass that happens to
  mirror the writable columns, not a mapped entity — it has no identity, no
  session, and no relationship to a row that exists.
- **The `**fields` wrapper design is rejected**, for the three reasons under
  *Decision*. Recorded because it is the obvious cheap answer and will be
  proposed again otherwise.

## Open questions

1. **Recovering the id.** Today both functions re-`SELECT` after writing.
   `result.inserted_primary_key` is populated for a plain SQLite insert, but its
   behaviour under `ON CONFLICT DO UPDATE` (where the row already existed and
   `lastrowid` may be stale or zero) needs checking against SQLAlchemy 2.0.50
   before relying on it. The safe version keeps the trailing `SELECT` for the
   upsert path and uses `rowcount` only to distinguish inserted-vs-conflicted in
   the `DO NOTHING` path. **Start safe** — keep the `SELECT`, drop only the
   *leading* one — and treat dropping the trailing `SELECT` as a separate,
   test-backed follow-up. The leading `SELECT` in `insert_document_if_new` is
   the check-then-act race and is the one worth removing.
2. **Does `rowcount == 0` reliably mean "conflicted" on SQLite `DO NOTHING`?**
   Believed yes via `pysqlite`; must be asserted by a test, not assumed, because
   the whole `insert_document_if_new` contract rests on it. If it proves
   unreliable, fall back to the existing leading `SELECT` and accept that
   `insert_document_if_new` keeps three statements — the column-list collapse,
   which is the actual TODO item, does not depend on this.
3. **Is `_OVERWRITE_ON_UPSERT` exactly right?** Derived by reading today's
   `DO UPDATE` clause: `title`, `url_or_path`, `fetch_status` overwrite;
   `item_type`, `zotero_attachment_key`, `note`, `doi`, `arxiv_id`, `isbn`,
   `year`, `authors_json`, `zotero_url`, `zotero_path` COALESCE. That is the
   full current set, so the policy reproduces present behaviour exactly. Worth a
   second look at `fetch_status`, though: overwriting it means a metadata re-run
   can move a document backwards from `fetched` to `pending`. That is existing
   behaviour and this change must preserve it — but it belongs on `TODO.md` as
   its own question rather than being quietly fixed here.
4. **Does `merge()` belong in this change?** *Decision* argues dedup needs it,
   but nothing calls it yet and an unused method is speculative. Recommendation:
   **no** — ship `DocumentWrite` as a value type, and let the dedup pass add
   `merge()` when it has a real caller to shape it.
