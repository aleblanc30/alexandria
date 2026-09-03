# Audit command reference

Run from repo root. `PY` below is the project venv's python
(`.venv/Scripts/python.exe` on Windows, `.venv/bin/python` on WSL). From a
worktree, prefix with `PYTHONPATH="$(pwd)"` — the venv is an editable install
pointing at the **main** checkout, so a worktree's edits are otherwise invisible
(CLAUDE.md, *Worktree venv shadowing*).

## Tool install — scratch only

Never add audit tools to `pyproject.toml`. Install them out of tree:

```bash
"$PY" -m pip install --target "$SCRATCH/audittools" radon vulture pylint
PYTHONPATH="$SCRATCH/audittools" "$PY" -m radon cc pka
```

mypy and ruff are already in `[dev]` and are run from the project venv.

## Scale baseline

| What | Command |
|---|---|
| Backend physical/logical lines, module count | `radon raw pka -s` (read the SLOC + LLOC totals) |
| Test lines and file count | `radon raw tests -s` |
| Frontend lines | `find frontend/src -name '*.ts' -o -name '*.vue' \| xargs wc -l \| tail -1` |
| Per-file churn (the ranking signal) | `git log --pretty=format: --name-only -- 'pka/*' 'frontend/src/*' \| sort \| uniq -c \| sort -rn \| head -30` |

## Static analysis

| Check | Command | Used for |
|---|---|---|
| Project lint gate | `ruff check pka tests scripts` | must be clean; anything here is a bug, not a finding |
| Format drift | `ruff format --check pka tests scripts` | M-item if any file drifted |
| Audit-only rules | `ruff check pka --select C901,PERF,SIM,PLR09,ARG,RET,TRY,BLE,S110,S112 --statistics` | complexity, blind excepts, long signatures, silent `pass` |
| Cyclomatic complexity | `radon cc pka -s -n C --total-average` | the CC ≥ 11 review list; `-n F` for the worst |
| Maintainability index | `radon mi pka -s -n B` | MI < 20 note, < 10 headline |
| Types | `mypy pka` | count errors by code; note pydantic-plugin false positives |
| Dead code | `vulture pka --min-confidence 70` | triage pydantic validators out by hand |
| Duplication | `pylint pka --disable=all --enable=duplicate-code --min-similarity-lines=10` | expect it to cluster in one family |
| Import cost | `"$PY" -X importtime -c "import pka.api.main" 2>&1 \| sort -k2 -rn \| head -25` | cumulative µs column; look for sklearn/chromadb/torch |
| Function-level imports | `grep -rnE "^\s{4,}(from\|import) pka" pka \| wc -l` then group by file | cycle smell + deliberate lazy-loading |

## Test and coverage health

```bash
pytest --durations=25
pytest --cov=pka --cov-report=term-missing
```

`fail_under = 85` means `--cov` exits non-zero on a coverage dip even with a
green suite — that exit code is a finding, not a broken run. Plain `pytest`
applies no threshold. Record wall time and note WSL is ~3× faster than Windows.

Frontend:

```bash
cd frontend && npm run test && npm run build
```

## Schema and query inspection

Index coverage, by hand — there is no tool for this:

```bash
grep -n "sa.Index\|ForeignKey\|document_id" pka/db/schema.py
grep -rn "CREATE INDEX IF NOT EXISTS" pka/db/queries.py
```

For each child-table column with no index, find its readers
(`grep -rn "<column>" pka/db pka/api`) and check whether any sit inside a
correlated `EXISTS`, a per-row join, or a browse/search path.

`EXPLAIN QUERY PLAN` is the confirming measurement, but running it needs a real
archive — so **name it in the finding, do not run it**. "SCAN source_tags"
becoming "SEARCH … USING INDEX" is the pass condition to state.

## Where to look, by finding class

| Class | Start here |
|---|---|
| Query shape, N+1, over-fetch | `pka/db/queries.py`, `pka/api/document_serialize.py`, `pka/api/routers/search.py` |
| Indexes / migrations | `pka/db/schema.py`, `init_db` in `pka/db/queries.py` |
| Startup cost | `pka/api/main.py` router list, then each router's module-level imports |
| Per-document round trips | `pka/ingestion/core.py`, `pka/clustering/doc_embeddings.py`, `pka/tag_training/lifecycle.py` |
| Repeated source reads | `pka/ingestion/progress/baselines.py`, `pka/ingestion/pending_metadata.py`, the SSE handler in `routers/ingestion.py` |
| Threading / background work | `pka/api/routers/ingestion.py` worker registry, `pka/clustering/engine.py` label pool |
| Frontend drift | `frontend/src/api/client.ts` vs the pydantic schema modules |
