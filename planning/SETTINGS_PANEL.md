# Settings panel — phase 2 (writes)

Phase 1, the read-only `/settings` report (`pka/api/settings_view.py`,
`routers/settings.py`, `SettingsView.vue`), has shipped. This is the write half,
tracked in `BACKLOG.md` → *Configuration*. Do it only once phase 1 has been used
enough to show which fields actually get re-set.

## Scope: the operational tier only

| Tier | Fields | Editable? |
|------|--------|-----------|
| Install-time | `data_dir`, `secrets_file`, `dev`, `dev_ingestion_limit_*` | **Never.** `data_dir` derives `archive.db`, `chroma/` and the snapshots; rebinding it under open SQLite/Chroma handles is a footgun, on Windows a file-locking one. Source paths keep their existing per-source picker on `/ingestion/:source`. |
| Operational | providers, models, base URLs, the §1.1 outbound flags, `ocr_enabled` / `clip_enabled` / `image_gate_enabled` | **Yes** — this phase. |
| Tuning | chunking, fetch timeouts and caps, gate thresholds, clustering | **No.** `.env.example`'s comments explain these knobs; a form would lose that prose or duplicate it into `Field(description=...)`. Per-run knobs go in run dialogs, as `ClusterRunDialog.vue` does. |

**Credentials are never accepted over HTTP.** They stay in `.secrets`
(`DESIGN.md` §1.1); taking an API key through an unauthenticated local API and
writing it to disk from the web tier weakens that story to save a one-time paste.

## Shape

- `PUT /settings/{field}` with `{value}`. Validate by constructing a throwaway
  `Settings(**{field: value})` so the field's own validators run (`_parse_bool`,
  `_expand_and_check`) before anything is persisted.
- 400 for any field outside the operational tier, and for any secret field (the
  message names `.secrets`). The allowlist is the gate, not the caller.
- Persist with `_persist_env_var`, **lifted from `pka/api/source_paths.py` into a
  shared `pka/api/env_file.py`** together with `ENV_FILE_PATH`, so the existing
  test override keeps working; `source_paths.py` re-imports both and does not
  change behaviour.
- `setattr(settings, field, coerced)`, then `reset_providers()` after a
  provider / model / base-URL change so cached instances rebuild on next use.
- **Label the fields whose effect is not live**, in the response and the UI: the
  EasyOCR reader caches independently of `reset_providers`, and the Chroma
  collection is dimension-locked, so an embedding-model change needs a
  `rebuild_from_chunks` reindex, not a toggle. A switch that silently does nothing
  is worse than no switch.

Writing through the panel also removes the unknown-key startup failure
(`INSTALL.md` §4, §11): the panel can only emit keys that exist on `Settings`.

## Docs and tests

- `DESIGN.md` §1.1: the panel may write outbound flags to `.env`, never
  credentials.
- Tests mirror `tests/test_source_paths.py`: allowlist and secret rejections,
  validator errors surface as 400 with nothing persisted, `reset_providers` is
  called for provider fields, `ENV_FILE_PATH` is redirected so no test touches a
  real `.env`.

## Out of scope

Editing `data_dir` or derived paths; displaying or editing credential values; a
form over the tuning tier; auth on these endpoints (the API is unauthenticated
and loopback-bound by design — if that changes, it changes app-wide).
