---
name: maintainability-performance-audit
description: Run a repeatable maintainability and performance audit of the Alexandria codebase and write it up as a numbered planning document (M-n / P-n findings). Use when asked to audit, review, or health-check the codebase for complexity, module size, coupling, duplication, dead code, test health, typing, exception hygiene, slow queries, missing indexes, startup cost, or N+1 — or to refresh an earlier audit against a newer commit. Not for security review (use the security-review skill) and not for reviewing a single diff (use code-review).
---

# Maintainability & performance audit

Produces one document: `planning/MAINTAINABILITY_PERFORMANCE_AUDIT.md` (or a
dated sibling when refreshing). The output is a **proposal**, like everything
else in `planning/` — it must say so, and it must not claim to be authoritative
about current behaviour.

The previous run is the reference implementation. Read it before starting: it
fixes the shape, the tone, and the numbering that `TODO.md` / `BACKLOG.md` lines
point back at.

## Rules that shape the whole audit

- **No profiling.** CLAUDE.md forbids running real ingestion, and there is no
  throwaway archive to time against. Every performance finding argues from code
  shape — query plans SQLite is forced into, imports at module scope, per-item
  round trips. Say so in the preamble, and for each P-item name the measurement
  that would confirm or demote it.
- **Never touch port 8420.** No starting servers, no `alexandria dev`.
- **Install audit tools into a scratch venv or `--target` directory**, never
  into the project. `pyproject.toml` and the lockfile stay untouched by an
  audit.
- **Read-only.** An audit produces one markdown file and nothing else. Fixes
  are separate follow-up work, sequenced by §6 of the report.
- Findings that duplicate an existing `planning/` item say so and add evidence
  instead of re-proposing (see M-4 in the last run).

## Procedure

### 1. Pin the ground

Record the commit and version being audited (`git rev-parse --short HEAD`, the
version from `pka/__init__.py` / `pyproject.toml`) and the date. Reread
`CLAUDE.md`, `DESIGN.md` §1.1 and §3, and skim `planning/TODO.md` +
`planning/BACKLOG.md` so you can cross-reference rather than duplicate.

Get the size baseline too — backend physical/logical lines and module count,
test lines and file count, frontend TS/Vue lines. Scale is what makes a
"1,620-line module" mean something.

### 2. Run the measurement pass

`references/commands.md` holds every command with its exact flags, plus what
each output is used for. Run them all — the value of the audit is that the
table in §1 of the report is reproducible, and a skipped check silently drops
a finding class.

Two rules for reading the numbers:

- **Thresholds, not vibes.** CC ≥ 11 is the review list, CC > 15 is a finding;
  maintainability index < 20 is B (note it), < 10 is C (a headline item);
  duplication over ~5–10 % of a family of modules is a finding.
- **Churn × complexity is the ranking signal**, not complexity alone. A
  1,200-line module nobody edits is not the problem. Pull commit counts per
  file and rank by the product; the last audit's top three (`clustering/engine.py`,
  `db/queries.py`, `routers/search.py`) all scored high on both.

### 3. Read the hot spots the numbers point at

The tools locate; they do not diagnose. For each candidate, open the file and
find the *seam* — the thing that makes the finding actionable:

- Existing section banners or `# ── Step N` comments are the split lines.
- Wide tuple returns, long keyword-parameter lists, and hand-mirrored default
  values are dataclass boundaries.
- A function that interleaves N named concerns extracts into N pure functions
  over a small shared type.

A finding without a named seam is an observation, not a recommendation.

### 4. Work the performance checklist

Argue each from the code, in this order (it is roughly highest-ratio first):

1. **Indexes.** List every FK-ish column in `pka/db/schema.py`, then grep for
   its readers. A column used inside a correlated `EXISTS` on a browse or
   search path with no index is the highest-payoff finding in the codebase.
   Remember `create_all` does not add indexes to existing tables, so a fix is
   *both* a `sa.Index(...)` and a `CREATE INDEX IF NOT EXISTS` in `init_db`.
2. **Import cost.** `python -X importtime -c "import pka.api.main"` and read
   the cumulative column. Heavy scientific libraries at module scope
   (sklearn, chromadb, torch, umap, hdbscan) behind a router import are startup
   tax on every reload.
3. **Per-item round trips.** Walk the shared ingest tail
   (`ingestion/core.py::ingest_text_block` → `doc_embeddings` →
   `tag_training`) counting SQLite transactions and Chroma calls per document,
   and note where a runner walks it twice.
4. **Over-fetch.** `select(documents)` that reads blob columns
   (`doc_embedding`, `generated_summary`) to use three scalars; unbounded
   `LIKE '%q%'`; over-fetch factors that grow with page depth.
5. **Repeated source reads.** Anything on a timer (the SSE progress stream)
   that re-opens `places.db`, the Calibre library, or an image directory.
6. **Async/threading hygiene.** Sync `def` handlers are *correct* for this sync
   SQLAlchemy stack — verify rather than flag. Check `async def` handlers do
   only in-memory work or `run_in_threadpool`, and check background-thread
   registries for unbounded growth.

Record what you checked and found fine as its own numbered item (the last run's
P-8). It is what stops the next audit re-deriving it.

**Two checks before any performance finding is written up.** The last audit's
P-6 failed both and had to be withdrawn — it claimed the SSE stream re-read
`places.db` every second, when the probe had been TTL-cached for a fortnight,
at the very TTL the finding went on to recommend:

- *Frequency is not inherited down a call chain.* Reaching an expensive leaf
  proves it is expensive, not that it runs at the caller's rate. Whenever a
  premise is "X happens N times per second", read X itself for memoisation,
  batching, or an early return before believing the multiplication.
- *`git log -S` the fix, not just the problem.* Search the tree for the
  mechanism you are about to recommend (`grep` the setting name, the cache, the
  index) and check its history. A recommendation that already shipped is worse
  than no finding: it sends someone to reimplement working code.

When a finding does turn out to be wrong, mark it **withdrawn** in place with
the correction and its date rather than deleting it — the `M-n`/`P-n` numbers
are load-bearing for `TODO.md` back-references, and the reasoning error is
itself worth leaving in the file.

### 5. Write the report

Structure, in order:

1. **Preamble** — commit, date, method limits, the "this is a proposal" line,
   the `M-n`/`P-n` numbering contract.
2. **§1 Method** — axes covered, and the tools table: one row per check, one
   line of result. This is the reproducibility contract.
3. **§2 Headline** — what is genuinely good (say it plainly and specifically),
   then a numbered short list of where the problems concentrate.
4. **§3 Maintainability findings** `M-n`, ordered by expected payoff, each with
   an S/M/L effort tag, an **Evidence** paragraph of hard numbers and
   `file.py:line` citations, and a **Recommendation** naming the seam.
5. **§4 Performance findings** `P-n`, same shape, each ending in the
   measurement that would confirm it.
6. **§5 Test-suite health** — runtime, slowest groups with a hypothesis for
   each, coverage against the 85 % gate, and which modules are under it.
7. **§6 Prioritised plan** — three tiers: quick wins (an afternoon, no design
   change), medium (a focused day or two), larger (needs its own
   `planning/<NAME>.md` per the repo convention).
8. **§7 Out of scope** and **References consulted** with live links.

Cite `file.py:line` for every claim. A finding whose evidence is a number
nobody can re-derive is the failure mode this skill exists to prevent.

### 6. Land it

Write the file to `planning/`, then add one line per adopted item to
`planning/TODO.md` (high priority) or `BACKLOG.md` (nice-to-have), each
referencing its `M-n`/`P-n` id so the two stay linked. Do not edit code, and do
not create a commit unless asked.
