# Settings panel

Plan for a UI surface over `pka/config.py`. No `TODO.md` entry exists yet — the
*What/why* below argues for one narrow high-priority slice (read-only) and one
backlog slice (writes), so the entries land in different files.

## 1. What already exists

There is **no settings router and no settings view**. Configuration is
hand-edited `.env` + `.secrets`, and `Settings` rejects unknown `ALEXANDRIA_*`
keys, so a stale key is a startup failure rather than a warning — the sharp edge
`INSTALL.md` documents twice (§4 and §11).

But the write machinery is **not** greenfield. `pka/api/source_paths.py` already
does the whole job for one slice of config:

| Piece | Location |
|-------|----------|
| `_persist_env_var(key, value)` — rewrite-or-append `KEY='value'` in `.env`, every other line preserved | `pka/api/source_paths.py:68` |
| `ENV_FILE_PATH` — module-level, rewritten by tests so a run never touches the real `.env` | `pka/api/source_paths.py:24` |
| Live mutation of the singleton — `setattr(settings, spec.field, path)` then persist | `pka/api/source_paths.py:81` |
| `GET`/`PUT /ingestion/sources/{source}/path`, plus the image-dir list endpoints | `pka/api/routers/ingestion.py:153` |
| Tests | `tests/test_source_paths.py` |

So "a settings panel" is not a new capability. It is **generalising a pattern
already in the tree** from four path fields to a chosen subset of the other 81,
plus the read surface that does not exist at all.

Two more relevant facts:

- Providers are cached for the process lifetime (`_chat`, `_vision`,
  `_gate_vision`, `_ocr`, `_embedder` in `pka/providers/__init__.py`), with
  `reset_providers()` already written and wired into `conftest.py`. That is the
  invalidation hook a live provider switch needs — it exists, it is just
  test-only today.
- `OllamaChatProvider.resolve_model` already probes `GET {base_url}/api/tags`
  with a 5s timeout and logs a warning on failure
  (`pka/providers/ollama.py:92`). A reachability check is a re-use, not new
  network code.

## 2. Why — and why the read half is worth more

`cfg.` is touched in 132 places across 81 distinct settings, and **nothing in
the UI reflects any of it**. There is no health endpoint anywhere in
`pka/api/`. The archive can be configured to route chat at OpenRouter, vision at
a local `llava`, OCR at EasyOCR on CPU, with CLIP off and three outbound flags
on — and the only way to see that is to read `.env` next to `config.py`'s
defaults and work out which won.

That is already a live problem: `TODO.md` carries **"Summarization calls fail
silently"** under *Ingestion*. A panel that renders

```
chat        ollama        model: (auto)   http://localhost:11434   unreachable
```

is the direct diagnostic for that item. The read half needs no write path, no
restart semantics, and no credential handling — it is a router, a schema and a
view, and it removes the single largest blind spot in the app.

The write half is genuinely useful but much narrower (see §6), and it is
competing with dedup, selective purge, and the image-gate classifier. It goes to
`BACKLOG.md`.

## 3. Design decisions

**Three tiers, and only the middle one is ever editable from the browser.**

| Tier | Fields | Surface |
|------|--------|---------|
| **Install-time** | `data_dir`, `secrets_file`, `dev`, `dev_ingestion_limit_*` | Displayed read-only, forever. `data_dir` derives `archive.db`, `chroma/`, the source snapshots and the OAuth token; rebinding it under open SQLite/Chroma handles is a footgun, and on Windows a file-locking one (CLAUDE.md *Pitfalls*). Source paths stay where they are — the existing per-source picker on `/ingestion/:source` is the right home for them, not a second control here. |
| **Operational** | providers, models, base URLs, the §1.1 outbound flags, `ocr_enabled` / `clip_enabled` / `image_gate_enabled` | Read now, editable in phase 2. |
| **Tuning** | chunking, fetch timeouts and caps, gate thresholds, clustering, `*_poll_interval_seconds` | Read-only, grouped and collapsed. Not worth a form (see below). |

**Credentials are never editable and never returned.** `DESIGN.md` §1.1 puts
them in `.secrets` deliberately. Accepting an API key in the panel routes it
through a browser into a local API with no auth and writes it to disk from the
web tier — a real weakening of the current story to save a one-time paste. The
panel reports **presence only**: `openrouter_api_key: set` / `not set`. The
value never leaves the process. Redaction is by field, matched on a suffix list
(`*_api_key`) plus the explicit `reddit_feed_url`, which is itself the
credential and is already redacted in logs.

**Tuning knobs do not get a form.** `.env.example` is 171 lines, **155 of them
comments** — why `clip_enabled` is off (a ~600 MB download for a narrow query
slice), why `easyocr_canvas_size` is 0 (turning it up evicts the gate VLM from a
4 GB card), why `book_summary_enabled` is split from `bookmark_summary_enabled`.
A form loses all of that unless the prose is migrated into
`Field(description=...)`, which duplicates a file that would then have to be
kept in sync. Read-only display costs nothing and keeps `.env.example`
authoritative.

**Per-run knobs belong in the run dialog, not in global config.**
`ClusterRunDialog.vue` already set that precedent by passing clustering
parameters as `TriggerRunRequest` fields. Anything that varies per run
(fetch depth, gate threshold) follows that pattern if it needs UI at all.

**One route, two phases.** The route is `/settings` from the start; phase 1
renders a read-only *Environment* report inside it and phase 2 makes the
operational section editable in place. Shipping as `/environment` and renaming
later churns the sidebar, the router and every bookmark for no gain.

**Reachability is probed on demand, never on mount.** A page that fires five
provider probes on every mount adds outbound calls to a page load — exactly the
implicit-escalation shape §1.1 forbids. Local Ollama is probed on mount
(`localhost` only, so a fresh checkout with no `.env` still makes no external
call); remote backends are probed only when the user clicks *Check*, and the
button says so.

## 4. Backend — phase 1 (read)

### 4.1 `pka/api/settings_view.py` (new)

Pure functions, no FastAPI import, mirroring how `source_paths.py` keeps the
logic out of the router:

- `SECRET_FIELD_SUFFIXES = ("_api_key",)` and `SECRET_FIELDS = {"reddit_feed_url"}`
  — `is_secret_field(name)` is the single definition of what must not be
  serialised.
- `GROUPS: dict[str, tuple[str, ...]]` — the section layout (`Providers`,
  `Outbound`, `Images`, `Fetch`, `Chunking`, `Clustering`, `Storage`, `Dev`),
  listing field names in display order. A field absent from every group is a
  test failure (§7), so a new setting cannot silently go unlisted.
- `build_settings_report()` → per field: `name`, `value` (or `null` when
  secret), `is_secret`, `is_set` (secret only), `is_default` (compares against
  `Settings.model_fields[name].default`), `tier`. `is_default` is what makes the
  page readable — the handful of fields that differ from stock is the actual
  answer to "what is this instance doing".
- `build_capability_report()` → for each of chat / vision / gate-vision / OCR /
  image-embed: the configured provider name, the resolved model, the base URL,
  and whether the credential it needs is present. Resolution mirrors
  `_build_*` in `pka/providers/__init__.py`; it must **not** call the
  `get_*_provider()` accessors, because those populate the module cache as a
  side effect and would pin a provider built for a page view.
- `probe_provider(capability)` → `{reachable: bool, detail: str}`. For Ollama
  routes, `GET {base_url}/api/tags` with a 5s timeout — same call
  `resolve_model` already makes. For OpenAI-compatible routes, `GET
  {base_url}/models` with the key. Exceptions are caught and returned as
  `detail`; this endpoint never 500s on an unreachable backend, because "it is
  down" is the answer, not an error.

### 4.2 `pka/api/schemas/settings.py` (new)

`SettingField`, `SettingGroup`, `CapabilityStatus`, `SettingsReport`,
`ProbeResult`. Plain pydantic models, matching the style of
`schemas/ingestion.py`.

### 4.3 `pka/api/routers/settings.py` (new)

`router = APIRouter(prefix="/settings", tags=["settings"])`

- `GET /settings` → `SettingsReport` (groups + capabilities, no probes).
- `POST /settings/probe/{capability}` → `ProbeResult`. POST, not GET: it makes
  an outbound call, so it must not be something a crawler or a prefetch can
  trigger.

Register in the `from pka.api.routers import (...)` list and the `for router in
(...)` loop in `pka/api/main.py` — both are alphabetical-ish groupings, add
`settings` alongside `search`.

## 5. Frontend — phase 1

- `frontend/src/api/client.ts` — `SettingField`, `SettingGroup`,
  `CapabilityStatus`, `SettingsReport`, `ProbeResult` interfaces plus
  `getSettings()` and `probeCapability(name)`, following the existing
  `DomainTopLists` pattern.
- `frontend/src/views/SettingsView.vue` (new) — a capability table at the top
  (capability, provider, model, endpoint, credential, a *Check* button per row
  writing its result into local state), then one `<details>` per group. Fields
  differing from their default are marked; secrets render `set` / `not set` and
  never a value. No store — the data is read once per mount and is not shared
  with any other view, so a `ref` in the component is right; `stores/` is for
  cross-view state (`browse`, `ingestion`, `ui`).
- `frontend/src/router.ts` — `{ path: '/settings', component: () =>
  import('@/views/SettingsView.vue') }`.
- `frontend/src/components/AppSidebar.vue` — a `RouterLink` in the lower group,
  next to *Reading lists*, with an inline `IconSettings` following the existing
  inline-`svg`-object convention at the bottom of that file.

## 6. Phase 2 — writes (backlog)

Only the **operational** tier, and only after phase 1 has been used enough to
know which fields are actually re-set often. Expected shape:

- `PUT /settings/{field}` with a body of `{value}`, validated by constructing a
  throwaway `Settings(**{field: value})` so the field's own validators run
  (`_parse_bool`, `_expand_and_check`) before anything is persisted.
- Reject any field not in the operational tier with a 400, and any secret field
  with a 400 naming `.secrets`. The allowlist is the gate, not the caller.
- Persist with `_persist_env_var` — **lifted out of `source_paths.py` into a
  shared module** (`pka/api/env_file.py`) rather than imported across from it,
  with `ENV_FILE_PATH` moving too so the existing test override keeps working.
  `source_paths.py` re-imports it; its behaviour must not change.
- `setattr(settings, field, coerced)`, then `reset_providers()` when the field
  is a provider/model/base-URL/key field, so the cached instances rebuild on
  next use. This is the part that makes a live switch honest.
- Fields whose effect is **not** live must be labelled as such in the response
  and in the UI: the EasyOCR reader is cached inside `pka/providers/easy_ocr.py`
  independently of `reset_providers`, and the Chroma collection is
  dimension-locked — changing the embedding model needs `rebuild_from_chunks`, a
  full reindex, not a toggle. A switch that silently does nothing is worse than
  no switch.

Writing through the panel also closes the unknown-key failure mode from §1: the
panel can only emit keys that exist on `Settings`.

## 7. Tests

`tests/test_settings_view.py` (new), mirroring `tests/test_source_paths.py`:

- **Every field on `Settings` appears in exactly one group.** This is the test
  that keeps the page from rotting — a new setting either gets a home or fails
  the suite.
- Secret fields serialise with `value=None` and a correct `is_set`; assert the
  actual key string appears nowhere in the JSON body.
- `is_default` is true for a stock `Settings()` and false for an override.
- `probe_provider` returns `reachable=False` with a `detail` (not an exception)
  when the endpoint refuses the connection — via the existing HTTP mock in
  `conftest.py`, never a real socket.
- Capability resolution does not populate the provider cache: assert the
  `_chat` / `_vision` / … module globals are still `None` after a report build.

`tests/test_api.py` gets the two endpoint smoke cases. Frontend: a
`SettingsView` test alongside the existing view tests, plus `npm run build` for
the typecheck.

## 8. Docs to update in the same commit

- `README.md` — the API surface / frontend views list gains `/settings`, and the
  configuration section (line ~203) should point at the panel as the way to
  *see* config, with `.env.example` still the way to set it.
- `DESIGN.md` §1.1 — a sentence that the outbound flags are surfaced read-only
  in the UI, and that the panel probes `localhost` on mount and remote endpoints
  only on explicit request. Phase 2 additionally needs §1.1 to state that the
  panel may write flags to `.env` but never credentials.
- `INSTALL.md` §4 — a pointer that the resulting configuration can be verified
  at `/settings` after `alexandria init`.
- `docs/ingestion-flows.md` and `docs/persisted-fields.md` — **no change**. This
  touches no pipeline phase, no outbound call inside a pipeline, and no column.
  Phase 2 does not change that: it edits the flags those graphs already colour,
  not where they sit.

## 9. Out of scope

- Editing `data_dir` or any derived path.
- Editing or displaying credential values.
- A form over the tuning tier.
- Auth on the settings endpoints. The API is unauthenticated and bound to
  localhost by design; adding a password to one router would imply a security
  property the rest of the surface does not have. If that changes, it changes
  app-wide, not here.
