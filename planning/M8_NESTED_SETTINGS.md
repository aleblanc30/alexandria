# M-8: group `Settings` where the field names already say so

Plan for `planning/MAINTAINABILITY_PERFORMANCE_AUDIT.md` §M-8 (item 12 of the
§6 prioritised plan). Step 1 of the audit's recommendation — replacing the
deprecated inner `class Config` with `model_config = SettingsConfigDict(...)` —
**already shipped** under M-7, so this covers only the nesting.

**This is a deliberately scoped version of the item.** The audit asks for a
full nesting of all 91 fields into invented group names (`settings.paths.*`,
`settings.providers.*`) with `env_nested_delimiter`. That version was written
out, costed, and rejected; see *Why not the full nesting* at the end. What
follows nests the **49 fields whose names already carry a group prefix**, which
turns out to cost no env-var migration and no compatibility machinery at all.

## Current state (verified against `trunk` @ `1aebf6e`)

- `pka/config.py` is 514 lines; `class Settings` declares **91 fields** flat,
  with `# ──` banners standing in for structure.
- 39 modules under `pka/` import it, 34 as `from pka.config import settings as
  cfg`, for **195 `cfg.<field>` reads**; 21 test modules hold **97
  `monkeypatch.setattr` calls** against the process-wide singleton.
- The four remote backends (`ollama_cloud`, `openrouter`, `ovh`, `scaleway`)
  declare **exactly the same four fields each** — `base_url`, `api_key`,
  `chat_model`, `vision_model`, 16 near-identical lines — and
  `api/settings_view.py` carries three parallel dicts (`_base_url_for`,
  `_api_key_for`, `_vision_model_for`) that are each a hand-maintained copy of
  the same provider-name mapping.
- `api/settings_view.py` also holds a hand-maintained grouping of all 91 fields
  (`GROUPS`, 95 lines), kept honest only by
  `test_settings_view.py::test_every_field_appears_exactly_once`.

## The rule that makes this free

Probed against the pinned pydantic-settings 2.14.1 / pydantic 2.13.4:

| Probe | Result |
|---|---|
| `env_nested_delimiter="__"`, legacy flat `ALEXANDRIA_FETCH_CONCURRENCY` → `fetch.concurrency` | **ignored**, field keeps its default |
| `validation_alias` on a field *inside* a nested model | **not consulted** — aliases resolve at the source top level only |
| `env_nested_delimiter="_"` + `env_nested_max_split=1`, `ALEXANDRIA_FETCH_TIMEOUT_SECONDS` → `fetch.timeout_seconds` | **works** |
| …with a **multi-word** group name: `ALEXANDRIA_IMAGE_GATE_TEXT_COVERAGE_MIN` → `image_gate.text_coverage_min`, `ALEXANDRIA_OLLAMA_CLOUD_API_KEY` → `ollama_cloud.api_key` | **works** — resolution is against declared field names, not a blind split |
| flat `image_dirs` alongside an `image_gate` submodel; scalar `dev` alongside a `dev_ingestion` submodel | **coexist** |
| group with an *invented* name (`paths`) against legacy `ALEXANDRIA_ZOTERO_DB` | **ignored**, keeps its default |

So the rule is sharp:

> **A submodel whose name matches the existing field prefix inherits every one
> of that prefix's legacy env vars for free. A submodel with an invented name
> inherits none of them.**

Nesting only the prefix-shaped groups therefore needs **no compat source, no
env renames, no `.env`/`.secrets` migration, and no deprecation cycle**. That
is the entire argument for this scope.

## The trap that must not be missed

Per-backend defaults **cannot** be expressed as defaults on the submodel
*instance*:

```python
openrouter: RemoteBackend = RemoteBackend(base_url="https://openrouter.ai/api/v1")  # WRONG
```

When env supplies *any* field of that submodel, pydantic-settings constructs a
**fresh** `RemoteBackend` from class-level defaults and discards the default
instance. Verified: setting only `ALEXANDRIA_OPENROUTER_API_KEY` silently
resets `openrouter.base_url` to `""`, and the next OpenRouter call fails with
"No base URL configured". Setting the API key is the single most likely thing
a user does, so this would break the production instance the first time it was
configured, with no error at import.

The fix is a one-line subclass per backend, so the default is **class-level**:

```python
class RemoteBackend(BaseModel):        # the shared shape
    base_url: str = ""
    api_key: str = ""
    chat_model: str = ""
    vision_model: str = ""

class OpenRouter(RemoteBackend):
    base_url: str = "https://openrouter.ai/api/v1"

class OllamaCloud(RemoteBackend):
    base_url: str = "https://ollama.com"

class Scaleway(RemoteBackend):
    base_url: str = "https://api.scaleway.ai/v1"

# ovh keeps base_url = "" (a region endpoint, no sensible default) -> plain RemoteBackend
```

Verified: partial env override now preserves every untouched default. The
same hazard does not apply to `fetch` / `cluster` / `reddit` / `image_gate` /
`easyocr`, whose defaults are all class-level by construction.

**A regression test for this belongs in `test_config.py`** — set one field of
each submodel from the environment and assert its siblings still hold their
declared defaults. It is the one failure mode of this change that is silent.

## Target shape — 49 of 91 fields

Every legacy env var below keeps working verbatim.

| Submodel | n | Leaves (prefix stripped) |
|---|---|---|
| `fetch` | 13 | `timeout_seconds`, `connect_timeout_seconds`, `concurrency`, `pdf_max_pages`, `pdf_max_bytes`, `pdf_timeout_seconds`, `pdf_budget_extra_seconds`, `wayback_fallback`, `wayback_extra_budget_seconds`, `wikipedia_retry_delay_seconds`, `wikipedia_max_retries`, `unfetchable_retry_after_seconds`, `user_agent` |
| `cluster` | 7 | `space`, `pca_components`, `assign_min_similarity`, `linkage`, `label_workers`, `async_labelling`, `regenerate_temperature` |
| `reddit` | 7 | `feed_url`, `user_agent`, `saved_limit`, `feed_poll_interval_seconds`, `feed_poll_jitter_seconds`, `feed_open_failed_page`, `archive_enabled` |
| `image_gate` | 4 | `enabled`, `text_coverage_min`, `vision_provider`, `vision_model` |
| `easyocr` | 2 | `gpu`, `canvas_size` |
| `ollama_cloud` | 4 | `base_url`, `api_key`, `chat_model`, `vision_model` — `OllamaCloud(RemoteBackend)` |
| `openrouter` | 4 | ″ — `OpenRouter(RemoteBackend)` |
| `ovh` | 4 | ″ — plain `RemoteBackend` |
| `scaleway` | 4 | ″ — `Scaleway(RemoteBackend)` |

The remaining **42 fields stay flat**, and that is the point rather than a
compromise: they share no prefix, so nesting them would mean inventing a group
name and losing their env vars. The resulting model is legibly half-nested —
*grouped exactly where the name already said so*.

Deliberately **not** grouped, with reasons:

- `chunk_sentences` / `chunk_overlap` but not `min_chunk_chars` — a group that
  cannot hold its own third sibling is worse than three flat fields.
- `search_provider` / `search_api_key` / `search_url_cards` — the first two are
  book-cover search, the third is a fetch behaviour. Same prefix, unrelated
  concerns; grouping them would assert a relationship that does not exist.
- `ollama_base_url`, the four capability selectors, the `*_summary_enabled`
  flags, the paths — no shared prefix.
- `dev` + `dev_ingestion_limit_*` — a `dev_ingestion` submodel does work
  (verified to coexist with the scalar `dev`) and would tidy
  `dev_limits._LIMIT_ATTR`. Six fields, one consumer. **Optional step 5**;
  drop it if the diff is already large enough.

## What still needs real work

Three of the four name-driven mechanisms from the full plan **fall away
entirely** under this scope, because every field they touch stays flat:

- `api/source_paths.py` — operates on `zotero_db`, `firefox_db`,
  `book_archive`, `youtube_client_secret`, `image_dirs`, all flat. Its
  `f"ALEXANDRIA_{spec.field.upper()}"` env-key derivation and both `getattr`
  sites stay correct. **Untouched.**
- `ingestion/core.py::_SUMMARY_FLAGS` — `bookmark_summary_enabled` /
  `book_summary_enabled`, flat. **Untouched.**
- `ingestion/dev_limits.py::_LIMIT_ATTR` — flat unless optional step 5 runs.

Two need changing:

### 1. `SecretsFileSettingsSource` (`config.py:83`)

Five secrets move under a submodel — `SECRET_ALEXANDRIA_OPENROUTER_API_KEY`,
`…_OVH_API_KEY`, `…_SCALEWAY_API_KEY`, `…_OLLAMA_CLOUD_API_KEY`,
`…_REDDIT_FEED_URL`. The current source matches parsed keys against
`settings_cls.model_fields`, so after the move each one hits the
`name not in fields` branch and is dropped with a "does not match any setting"
warning. **A dropped API key is silent at import** — this is the same class of
failure as the instance-default trap.

Do not patch the lookup — **subclass `EnvSettingsSource` instead**:

```python
class SecretsFileSettingsSource(EnvSettingsSource):
    def _load_env_vars(self) -> dict[str, str]:
        path = _secrets_file_path()
        if path is None or not path.is_file():
            return {}
        # EnvSettingsSource lowercases keys when case_sensitive is False.
        return {k.lower(): v for k, v in parse_secrets_file(path).items()}
```

Verified: this resolves nested *and* flat secrets, preserves untouched
defaults, and keeps env > secrets > `.env` precedence. It **deletes** the
hand-rolled prefix matching, field lookup and `get_field_value` (~20 lines of
mechanism) and inherits pydantic's own nested resolution, so it stays correct as
the model grows — a genuine simplification of the file, not a patch to
accommodate one. Note the *file* barely shrinks (3 lines): the saving goes into
a docstring, and both warnings are worth keeping.

**Shipped ahead of the rest of M-8**, as step 4 anticipated — it is a no-op
against the flat model, so it carries none of the nesting's risk.

`parse_secrets_file` is unchanged, so its three direct tests pass untouched.
One behaviour is lost: the per-key "does not match any setting" warning. Re-add
it explicitly in `_load_env_vars` if it is worth keeping — `test_config.py`
asserts only that unknown keys are *ignored*, so no test forces the decision.

### 2. `api/settings_view.py`

`GROUPS` entries for the 49 become dotted paths (`"fetch.timeout_seconds"`),
and `_build_field` needs a resolver for both halves:

```python
def _resolve(name: str) -> tuple[FieldInfo, Any]:
    model, obj = Settings, cfg
    *parents, leaf = name.split(".")
    for part in parents:
        model, obj = model.model_fields[part].annotation, getattr(obj, part)
    return model.model_fields[leaf], getattr(obj, leaf)
```

`test_every_field_appears_exactly_once` changes from
`set(all_names) == set(Settings.model_fields)` to a **recursive leaf
enumeration** — strictly a stronger invariant than today's.

The three parallel provider dicts collapse to one accessor:

```python
def _remote(name: str) -> RemoteBackend | None:
    return getattr(cfg, name) if name in _REMOTE_PROVIDERS else None
```

`GROUPS` itself **survives** — it can only be deleted if every field is
nested, which is the full version. It stays a 95-line table; the recursive
test keeps it honest as it does today.

Emit the **dotted path** as the wire `name`, so the panel shows where a setting
lives. `SettingsView.vue` renders `f.name` and `g.name` generically and needs
no change.

## What this pays for

- **16 duplicated field declarations → 1 base model + 3 one-line subclasses**,
  and three hand-maintained parallel dicts → one accessor. This is the only
  part of M-8 that removes duplication rather than relocating it.
- `~45` lines of hand-rolled settings-source logic deleted by inheriting
  `EnvSettingsSource`.
- `settings.fetch.*`, `settings.cluster.*`, `settings.reddit.*` read as
  intended in the four modules that carry the concentration —
  `ingestion/fetcher.py` (23 of its 24 settings reads are `fetch_*`),
  `providers/__init__.py` (19), `connectors/reddit.py` (10).
- `test_every_field_appears_exactly_once` gets structurally stronger.

Not claimed: this does **not** reduce config.py's churn (see below), and does
**not** address "imported as a global by 31 modules" — the other half of M-8's
own headline. The 97 monkeypatches exist because `settings` is a process-wide
singleton, not because the namespace is flat; this change shortens 26 of them
and leaves the structure alone.

## Blast radius

|  | Scoped (this plan) | Full nesting (rejected) |
|---|---|---|
| Fields moved | 49 of 91 | 91 |
| `cfg.<field>` read sites | **110**, 15 modules | 195, 39 modules |
| Test monkeypatches | **26**, 7 files | 97, 21 files |
| Legacy env vars broken | **0** | 91 |
| New permanent machinery | **none** | compat source + `LEGACY_PREFIX` protocol + dotted-path resolver |
| `settings_view.GROUPS` | survives (95 lines) | deleted |

Read sites by module: `ingestion/fetcher.py` 23, `providers/__init__.py` 19,
`api/settings_view.py` 16, `connectors/reddit.py` 10, `ingestion/book_search.py`
6, `ingestion/fetch_base.py` 5, `image_pipeline.py` 4, `clustering/engine.py` 4,
`wikipedia.py` 3, then eight modules with 1–2 each.

Test patches: `test_connector_reddit.py` 11, `test_pending_metadata.py` 5,
`conftest.py` 4, `test_settings_view.py` 2, `test_image_gate.py` 2,
`test_purge_source.py` 1, `test_clustering.py` 1.

## Order of operations

1. **`RemoteBackend` first**, on its own. It is the highest-value, most
   self-contained piece: 16 declarations → 4, the three `settings_view` dicts
   → one accessor, ~40 read sites. Land the class-level-default regression
   test *with* it.
2. **`fetch`**, the largest single group and the one with the most concentrated
   consumer (`ingestion/fetcher.py`).
3. **`cluster`, `reddit`, `image_gate`, `easyocr`** — one group per step.
4. **`SecretsFileSettingsSource` → `EnvSettingsSource` subclass.** Can be done
   before step 1 as a pure no-op refactor against the current flat model,
   which is the safer ordering: it isolates "did the source rewrite break
   precedence?" from "did the move break resolution?"
5. *(Optional)* `dev_ingestion`.
6. **`settings_view`**: dotted paths in `GROUPS`, the `_resolve` helper, the
   recursive invariant test.
7. **Docs**: `.env.example` and `DESIGN.md` §1.1 need **no changes** — every
   env var name is unchanged. `CHANGELOG.md` gets a line noting the Python-side
   attribute rename for anyone scripting against `pka.config`.

`mypy pka` is the gate that catches a missed read site — a stale
`cfg.fetch_timeout_seconds` is an `AttributeError` at runtime, not at import,
and ruff will not flag it. Run `scripts/check.ps1` after each step, and keep
`pka/config.py` and `pka/api/settings_view.py` off the mypy override list.

## Verification

Beyond `scripts/check.ps1` per step:

- **New** `test_config.py::TestSubmodelDefaults` — for each submodel, set one
  field from the environment and assert every sibling keeps its declared
  default. This is the instance-default trap; without this test the change can
  ship broken and pass everything else.
- **New** `test_config.py::TestLegacyEnvNames` — assert all 49 pre-M-8 env var
  names still resolve to their new nested location. Generate the list from the
  submodel prefixes so it cannot drift.
- `test_config.py`'s six existing precedence tests must pass **unchanged in
  intent** across the source rewrite (their field references become nested).
- `test_settings_view.py` — the now-recursive grouping invariant.
- `test_source_paths.py` — should pass **completely untouched**. If it needs an
  edit, the scope has leaked into flat fields; stop and re-check.

## Why not the full nesting

Two findings from costing it:

1. **config.py's churn is growth, not rework.** Across its 45 commits: 613
   lines added, 96 deleted, net **+517**, median **8** lines per commit, and 15
   of 42 commits deleting nothing at all. The churn × complexity heuristic the
   audit cites (§References) reads churn as a *rework* signal; here it is a
   feature adding one setting at a time, which is a config file behaving
   correctly. Nesting removes none of that future churn — a new fetch setting
   is one new line either way. The "most-churned file in the repo" framing
   overstates the case for restructuring it.
2. **Invented group names cost a permanent compat layer.** `paths`,
   `providers`, `enrichment`, `chunking` match no existing env prefix, so
   nesting them breaks all 91 legacy names. Unknown env vars are not an error
   in pydantic-settings — they are ignored — so every miss is a setting
   silently reverting to its default on the production instance. Covering that
   needs a `LegacyFlatEnvSource`, a `LEGACY_PREFIX` protocol, a dotted-path
   resolver and a rewritten secrets source: **permanent new machinery in the
   very file the audit criticises for complexity**, bought to make a namespace
   read better.

The full version buys exactly one thing this one does not: deleting
`settings_view.GROUPS`. That is 95 lines of table already guarded by a passing
test, and it is not worth 85 extra read sites, 71 extra test edits, a 91-name
env migration and a permanent compat layer.

If the real driver turns out to be **test isolation** rather than readability,
neither version is the right change — a `get_settings()` accessor or injection
at the four heavy consumers is what addresses the singleton, and it was not
evaluated here. Worth a separate item if `conftest.py`'s autouse fixture keeps
causing trouble.

## Not in scope

- **Writable settings.** `SETTINGS_PANEL.md` §6's phase 2 is a separate item.
- **Changing any default, validator, or the `.secrets` file format.** This is a
  namespace change; `parse_secrets_file`'s contract is unchanged.
- **Any env var rename.** The scope is defined by which fields can move without
  one.
- `pyproject.toml`'s mypy override list — nothing should be added to it.
