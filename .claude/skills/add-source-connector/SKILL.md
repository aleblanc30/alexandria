---
name: add-source-connector
description: Add a new source connector to Alexandria (Pocket, Raindrop, Readwise, or any new ingestion source). Use when adding, scaffolding, or wiring up a new source of documents into the archive — connector module, Source enum, runner, sync entry points, registry, API, tests, and sidebar. Also use when auditing whether an existing connector is wired up completely.
---

# Adding a source connector

A new source touches nine places. Missing any one of them produces a connector
that half-works: it ingests but reports no progress, or it appears in the API but
not the sidebar. Work the checklist in order — later steps depend on earlier ones.

Read `DESIGN.md` §2 and §3 alongside this; §3 explains the two-phase model the
runner has to fit into.

## Checklist

### 1. Connector — `pka/connectors/<source>.py`

Expose `load_items()` returning a list of dataclass objects with at minimum
`source_id`, `title`, `tags`, `date_added`.

The connector **only reads the source**. It does no chunking, embedding, or DB
writes — that is the runner's job. Keep any third-party client lazy-imported
inside a helper so the module imports without the optional dependency installed
(see `pka/connectors/youtube.py`).

### 2. Enum — `pka/constants.py`

Add the source to the `Source` string enum. Every later step keys off this value.

### 3. Runner — `pka/ingestion/runners/<source>.py`

Metadata and embed steps, routing all text through `ingest_text_block()` from
`pka/ingestion/core.py`. Never re-implement chunking or embedding.

Use `existing_chunk_count()` to offset chunk indices when a second phase adds
text to a document that phase 1 already populated.

### 4. Sync entry points — `pka/ingestion/<source>_sync.py`

`sync_<source>_metadata` / `sync_<source>_ingest`, plus an optional
`sync_<source>` running the full pipeline.

### 5. Registry — `pka/ingestion/registry.py`

Register the handlers so the ingestion API can drive the source.

If the source discovers its work as it goes rather than knowing the corpus up
front, give it a `PhaseSpec` in `PHASE_SPECS`:

- `plans_own_phases=True` — the ingest sets phase totals as it discovers work
  (Firefox does this; it can't know the queue until the fetch list is built).
- `tracks_embedding=False` — fetch and embed run interleaved, so there is no
  separate embedding phase to report.

Skipping this when the source needs it is the most common cause of a progress
bar that sits at zero or jumps backwards.

### 6. Pending counts — `pka/ingestion/pending_metadata.py`

Add `count_pending_metadata()` coverage if the source is document-based.

**Status polls must not touch the network.** For a network source with no
credentials configured, return 0 and compute the real pending count inline
during the sync instead.

### 7. CLI — `pka/cli/<source>.py` + `scripts/run_<source>.py`

Add a `main(argv)` owning its own argparse parser, then register the subcommand
in the `COMMANDS` dict in `pka/cli/__init__.py`. `scripts/run_<source>.py` stays
a thin shim over it.

### 8. Tests — `tests/conftest.py` + `tests/test_connector_<source>.py`

Add a fixture mocking the new external boundary in `conftest.py`. The suite must
stay fully isolated — no real source database, no real network. Inject a fake
client/service rather than reaching for the real one.

### 9. Sidebar — `frontend/src/components/AppSidebar.vue`

Add the entry, plus anything source-specific in `frontend/src/constants/sources.ts`.

### 10. Flow graph — `docs/ingestion-flows.md`

Add a section for the new source: a mermaid graph in the same shape as the
others, plus a column in the *What is actually shared* matrix. Reuse the five
existing `classDef`s unchanged — the point of the document is that shared
machinery, source-specific code, outbound calls, persistence, and flag-gated
steps read the same colour in every graph.

Nothing tests this, so it is easy to skip and leave the document quietly wrong.
Copy the graph of whichever existing source your pipeline most resembles
(Zotero/YouTube for a metadata-only source, Firefox for one that fetches, Images
for one that infers its text) and change what actually differs.

## Network sources

A source that reaches an external API must satisfy the `DESIGN.md` §1.1 policy.
`pka/connectors/youtube.py` is the template (DESIGN §2.1); copy its shape rather
than arguing for an exception:

- **Inert by default.** No credentials configured → the source reports
  "unavailable" and every status poll stays network-free. A fresh checkout with
  no `.env` makes no calls beyond `localhost`.
- **Named, default-off setting.** No implicit escalation from some other flag.
- **Credentials in `.secrets`** (git-ignored), never in `.env.example` or code.
- **Read-only.** No writes back to the remote service. Telemetry and analytics
  are prohibited in every configuration.
- **Optional dependency** in a `pyproject.toml` extra, lazy-imported.

## Verify

```
pytest
ruff check pka tests scripts
cd frontend && npm run test && npm run build
```

Do **not** run the real ingestion (`alexandria <source>`) to check your work — it
touches real source databases and external services. The tests are the check.
