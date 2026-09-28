# CLAUDE.md

Instructions for AI coding agents working in this repository.

**Alexandria v0.0.11** — a local-first research library that ingests bookmarks,
papers, ebooks, saved posts/videos, and images into one semantically indexed
SQLite + ChromaDB archive. Backend is Python (`pka/`), frontend is Vue 3
(`frontend/`). Read `README.md` for setup and usage.

## Which document wins

1. **Code and tests** — the ground truth.
2. **`DESIGN.md`** — the living design spec (architecture, network policy §1.1,
   two-phase ingestion §3, retrieval enrichment §3.2).
3. **`README.md`** — human-oriented setup and usage.
4. **`docs/ingestion-flows.md`** — per-source ingestion flow graphs (mermaid),
   colour-coded shared vs. source-specific. **Derived, not authoritative**: it
   is a drawing of what the code does, so where it disagrees with `pka/`, the
   code is right and the graph is stale — fix the graph, and see the sync rule
   under *Pitfalls*.
5. **`docs/persisted-fields.md`** — what each source writes, per table and
   column. **Derived, not authoritative** for the same reason: `pka/db/schema.py`
   and the runners are what it reads from, so the code wins.

`docs/archive/initial_design.pdf` is **historical**: it records the March 2026
intent and is superseded wherever it disagrees with the above — notably, it
specifies local-*only* with no cloud dependencies, whereas the project is now
local-*first* (see `DESIGN.md` §1.1).

Work tracking and in-flight design proposals live in `planning/`, not at repo
root: `planning/TODO.md` (high-priority, one line each), `planning/BACKLOG.md`
(nice-to-haves), and one dedicated `planning/<NAME>.md` per larger plan (e.g.
`planning/MCP_PLAN.md`, `planning/WAYBACK.md`). None of these are
authoritative about current behavior — they describe proposed or deferred
work, not what the code does. Once a `planning/<NAME>.md` plan's corresponding
`TODO.md` line(s) are checked off with nothing left proposed, move the file to
`planning/archive/<NAME>.md` and repoint whatever still references it.

**Nothing outside `planning/` may cite it.** Code, comments, docstrings, tests,
config, scripts, `README.md`, `DESIGN.md` and `docs/` must not reference a
planning file, a section of one, or an audit item id (`M-14`, `P-9`). Plans get
condensed, renumbered and archived, so those citations rot, and they send the
reader to a proposal to understand shipped code. State the reason in the comment
itself, in a sentence or two; a decision that outlives its plan belongs in
`DESIGN.md`. `CHANGELOG.md` is exempt as a historical record, and within
`planning/` the files may cite each other freely.

## Boundaries

- **Do not run real ingestion.** `alexandria zotero|firefox|calibre|images|reddit|youtube`
  and the `scripts/run_*.py` shims touch real source databases and external
  services. Do not run `alexandria dev` either — it blocks until interrupted.
  Tests are how you verify ingestion changes.
- **Local-first / privacy.** Outbound paths are allowed but must follow
  `DESIGN.md` §1.1: a named setting, default off, no implicit escalation from
  another flag, credentials in `.secrets`. A fresh checkout with no `.env` must
  make no calls beyond `localhost`. **Telemetry and analytics are prohibited in
  every configuration.**
- **Do not commit** `.env`, `.secrets`, real database paths, or user data under `data/`.
- **Do not create git commits or PRs** unless asked. When asked, commit directly
  to `trunk` — no feature branch; this is a single-maintainer local repo.
- **Never stage with `git add -A`, `git add .`, or `git commit -a`.** Stage the
  files you actually touched, by path (`git add pka/foo.py tests/test_foo.py`).
  Other agents and the user may be editing the same worktree concurrently, so a
  blanket stage sweeps their unrelated in-flight edits into your commit. Check
  `git status` first and leave anything you did not write alone.
- **Do not edit** `.venv/`, `frontend/dist/`, `pka.egg-info/`, or generated caches.
- **Never stop, restart, or otherwise touch a process bound to port 8420.** It is
  the user's real production Alexandria instance with real ingested data, not a
  throwaway dev server — see *Ports* under Pitfalls.

## Verifying a change

Run from repo root.

**Default to `scripts/check.sh` (Bash/WSL) or `scripts/check.ps1` (PowerShell).**
It runs every check below in one pass — ruff check, ruff format --check, mypy,
the import-layering contract, pytest with coverage, `npm run lint`, `npm run test`, `npm run build` — using the repo's own
`.venv` directly, so no activation is needed. Every step runs even when an
earlier one fails, so one invocation reports everything that is broken; it exits
non-zero if any step failed. Use it in place of chaining the commands by hand.

| Task | Command |
|------|---------|
| **All checks at once** | **`scripts/check.sh` (Bash/WSL) or `scripts/check.ps1` (PowerShell)** |
| Backend tests | `pytest` |
| Backend tests + coverage | `pytest --cov=pka --cov-report=term-missing` |
| Lint / format | `ruff check pka tests scripts` / `ruff format pka tests scripts` |
| Type check | `mypy pka` |
| Import layering | `lint-imports` |
| Frontend lint | `cd frontend && npm run lint` |
| Frontend tests | `cd frontend && npm run test` |
| Frontend build + typecheck | `cd frontend && npm run build` |

Reach for the individual commands (with the venv active) only for a fast inner
loop — a single failing test, a quick re-lint — then finish with the script.
`pytest` is the minimum after backend changes; **all three** of `npm run lint`,
`npm run test` and `npm run build` after TypeScript/Vue changes. `mypy pka` is
baseline-ratcheted (`pyproject.toml`'s `[[tool.mypy.overrides]]`): modules with pre-existing errors are listed there with
`ignore_errors = true`, so the gate is "no new errors" outside that list, not a
clean `mypy` across the whole tree. None of this runs in CI or a hook — the
check scripts are deliberately manual-run.

Two configuration facts that otherwise read as bugs:

- Coverage enforces **`fail_under = 85`**, so `--cov` exits non-zero on a coverage
  dip even when every test passed. Plain `pytest` applies no threshold.
- ruff sets `line-length = 100` but **ignores `E501`** (the formatter handles
  wrapping), and exempts `B008` under `pka/api/routers/*` because FastAPI's
  `Depends(...)`-in-defaults style triggers it by design.
- ruff also selects `BLE001` / `S110` / `S112`, so a blind `except Exception` — and
  especially a silent `pass` or `continue` under one — fails the lint. Narrow the
  type, or log the failure, or write `# noqa: BLE001 - <why>` if the broad catch is
  a deliberate "one document must not kill the sync" guard.
- **Imports are layered** (`DESIGN.md` §1.2, `[tool.importlinter]` in
  `pyproject.toml`): a package imports only from layers below it, and a new
  top-level module must be placed in the contract. The `ignore_imports` list is
  a baseline that only shrinks — fix a new violation, never add it there.
  Tag training reacts to ingestion through `pka.hooks`, registered by
  `pka.bootstrap.install_hooks()`; a new entry point that runs ingestion must
  call it.
- ruff selects `ARG001` outside `tests/`: an unused function argument fails the
  lint. Remove it, or prefix it with `_` when a caller's signature requires it
  (a callback, a FastAPI hook, a dispatch table entry).
- **The frontend's API types are generated, not written.** `frontend/src/api/types.gen.ts`
  comes from `frontend/src/api/openapi.json`, which `python scripts/dump_openapi.py`
  writes from the live FastAPI app; `client.ts` aliases the generated schemas under
  the names it always exported. Change a request or response model and
  `tests/test_openapi_snapshot.py` fails until you run
  `python scripts/dump_openapi.py && (cd frontend && npm run gen:api)` and commit
  both files. Do not hand-edit either. A route with no `response_model` has nothing
  to generate from, so its shape stays hand-written in `client.ts`, in the block
  the comment there marks.
- ESLint (`frontend/eslint.config.js`) selects `@typescript-eslint/no-explicit-any`,
  so `catch (e: any)` fails; catch `unknown` and pass it to `errorMessage` /
  `notifyError` (`src/lib/notifyError.ts`), which do the narrowing in one place.
  eslint-plugin-vue runs on `flat/essential`, deliberately not `flat/recommended`,
  whose template-formatting rules would rewrite every component.

## Pitfalls

- **Windows file locking.** The primary dev environment is Linux/WSL, but the repo
  is also checked out on Windows. Close SQLite connections and temp files before
  renaming or reopening them — Windows locks open files.
- **`pka/pipeline.py` is deprecated.** It is a back-compat re-export shim; import
  from `pka.ingestion.core` / `pka.ingestion.runners` instead. Its
  `_ingest_text_block` is an alias for `ingest_text_block`.
- **Never bypass the test mocks.** `tests/conftest.py` redirects all data paths to
  `tmp_path` and mocks Ollama, ChromaDB, HTTP, and CLIP. A test that reaches a
  real database or the network is broken, not thorough.
- **Never delete a test to get a green suite.** Rework it only if the behavior
  stays covered, or the design is deliberately changing; otherwise ask for
  guidance. Deleting drops coverage and hides still-shipping behavior.
- **Schema changes** must keep `pka/db/init_db.py` idempotent — `alexandria init`
  is safe to re-run against an existing archive.
- **Ports.** The installed/production app defaults to 8420 (README's production
  section, `scripts/start-server.bat`). `alexandria dev`, the `vite.config.ts`
  proxy, and `.vscode/tasks.json`'s dev tasks use **8421** instead — deliberately
  different, so a source checkout's dev server never collides with (or, worse,
  proxies into) a real running production instance. `.vscode/launch.json` debug
  configs use yet another port, 8000.
- **Worktree venv shadowing.** `.venv` is an editable install (`pip install -e`)
  pointing at the **main** checkout's `pka/`. From a worktree (e.g. under
  `.claude/worktrees/<name>/`), `import pka` still resolves to the main repo, so
  the worktree's edits go untested unless you set `PYTHONPATH` to the worktree
  root when invoking that venv's python, e.g.
  `PYTHONPATH="$(pwd)" ../../../.venv/Scripts/python.exe -m pytest`.
- **`docs/ingestion-flows.md` must be updated in the same commit** as any change
  that alters what its graphs show. There is no test for this — a stale graph
  fails silently and misleads the next reader. Update it when you:
  - add or remove a source (the new pipeline needs its own graph, plus rows in
    the *What is actually shared* matrix and the §1 source list in `DESIGN.md`);
  - change a pipeline's **phase shape** — a `set_phase` / `skip_phase` call, a
    `PHASE_SPECS` entry, or a second pass over the same phase;
  - move logic **across the shared/source-specific line** — that boundary is the
    whole point of the colour coding, so promoting a runner helper into
    `ingestion/core.py` (or the reverse) changes a node's colour, not just its
    label;
  - add, remove, or re-gate an **outbound call** (red and purple nodes must match
    the `DESIGN.md` §1.1 flag that actually guards them);
  - change the **shared tail** — `ingest_text_block`, the chunker, `upsert_chunks`,
    `insert_chunks`, or `refresh_document_embedding` — which is drawn in all
    seven graphs and so must be corrected in all seven.

  Redrawing is cheap; read the source of truth in this order: `registry.py` for
  the handler map, `<source>_sync.py` for phases, `runners/<source>.py` for the
  text handed to `ingest_text_block`, `connectors/<source>.py` for the read.
- **`docs/persisted-fields.md` must be updated in the same commit** as any change
  to *what* a source writes. Same silent-staleness problem as the flow graphs,
  and a worse failure mode: its whole value is that a reader can trust a `—` to
  mean "nothing writes this". Update it when you:
  - add, remove, or rename a **column or table** in `pka/db/schema.py` (a new
    `documents` column is a new row in the §1 matrix, and usually a line in the
    per-source meaning table under it);
  - change **which sources write a column** — a `DocumentWrite` field in a
    runner, an `update_card_summary` / `set_fetch_status` call, a new field on
    `FetchResult` that `_persist_fetch_result` writes. A cell flipping between
    ✅/⬛/🟪/— is exactly the edit this file exists to catch;
  - add or remove a **source** (a column in all four matrices, alongside the
    flow-graph work above);
  - change the **chunk provenance mirror** in `ingest_text_block`, or the
    `extra_metadata` a runner passes — §3 lists both the SQLite `chunks` columns
    and the Chroma keys per `pass=`, so they move together;
  - change **what text is embedded** or its fallback (the §4 table), or add,
    remove, or re-gate a **flag-gated write** — `generated_summary`, the
    external synopsis, `archive_url` — whose flags must match `DESIGN.md` §1.1;
  - retire a **dead column or table** the file currently calls out (today
    `images.text_vector_id` and `image_tags`) — deleting it means deleting the
    note, not leaving a warning about code that no longer exists.

  Read the source of truth in this order: `db/schema.py` for columns,
  `db/documents.py::DocumentWrite` for what an ingestion upsert may touch,
  `runners/<source>.py` and `image_pipeline.py` for who writes what,
  `ingestion/core.py` for the chunk tail, `ingestion/fetcher.py` for the
  fetch-time writes.
- **No uniqueness claims about sets the code can grow.** In docs, comments and
  docstrings, do not call something *the only* / *the sole* / *the first*
  source, pipeline, caller, setting or target, say *no other* one does
  something, or rank it against the others (*the furthest from the shared
  shape*). The next addition makes the sentence false and nothing flags it: a
  flow-graph intro kept calling Calibre the only source with two embedding
  passes after Zotero gained one. Describe the thing's own property instead
  ("Firefox's `PhaseSpec` is …", "Zotero's read starts by snapshotting …"). A
  uniqueness that is a deliberate constraint belongs in `DESIGN.md`, stated as
  the rule, so changing it is a design decision; elsewhere, cite that section.
  Facts about external systems (what Reddit's feed serves) are not covered.
- Keep diffs minimal; do not refactor unrelated code.

## Where things live

| Area | Start here |
|------|------------|
| Ingestion / connectors | `pka/ingestion/core.py`, `pka/ingestion/runners/`, `pka/connectors/`, `DESIGN.md` §2–3 |
| Which pipeline does what (visual) | `docs/ingestion-flows.md` — one flow graph per source, shared spine vs. source-specific |
| What each source persists (tables) | `docs/persisted-fields.md` — column-by-source matrices for `documents`, the side tables, and the chunk/Chroma payload |
| Model backends (local + hosted) | `pka/providers/`, `DESIGN.md` §1.1 |
| Search / vectors | `pka/storage/vector_store.py`, `pka/api/routers/search.py` |
| Clustering | `pka/clustering/engine.py` (orchestrator: pipelines + `run_clustering`), then its step modules — `embeddings.py`, `reduce.py`, `hdbscan_step.py`, `agglomerative.py`, `labelling.py`, `persist.py`, shared `types.py` — plus `pka/clustering/lifecycle.py` |
| Sync progress / SSE | `pka/ingestion/progress/` (`state` → `tracker` → `view`/`baselines`) |
| Images | `pka/ingestion/image_pipeline.py`, `image_extractor.py`, `image_gate.py` |
| Image search (CLIP vs inferred text) | `DESIGN.md` §3.3, `pka/api/image_hits.py` |
| Retrieval enrichment | `DESIGN.md` §3.2, `pka/ingestion/openlibrary.py` |
| API surface | `pka/api/main.py` (router list), matching router + schema modules |
| Frontend views | `frontend/src/router.ts`, relevant store + `api/client.ts` |
| Tag training / trends | `pka/tag_training/`, `pka/trends/` |
| Backlog, todo, in-flight plans | `planning/` — `TODO.md`, `BACKLOG.md`, one file per larger plan |

Adding a new source connector: use the **`add-source-connector` skill**.

<!-- Path-scoped rules in .claude/rules/ carry the Python, frontend, and test
     conventions; they load when Claude reads a matching file. Anything whose
     violation is costly is duplicated above, since rules are not re-injected
     after /compact. -->
