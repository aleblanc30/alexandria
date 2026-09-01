# Image gate: CLIP-backbone classifier instead of the VLM

**Status:** proposed — not implemented.

The image admission gate (`pka/ingestion/image_gate.py`) currently spends a
per-image VLM call (Ollama `moondream`) to decide whether an image is a category
of interest. This replaces that call with a **frozen CLIP backbone + a trainable
linear head**, trained from labelled folders on disk via a CLI command.

## 0. What already exists (most of the backbone is built)

| Piece | Where | Reused how |
|---|---|---|
| CLIP image encoder | `pka/providers/clip.py::ClipImageEmbedder.embed_image` | **This is the frozen backbone.** Returns a 512-d L2-normalised vector, `openai/clip-vit-base-patch32`, offline-first cached load. No new dependency, no new download. |
| Linear head + JSON persistence | `pka/tag_training/engine.py:42-57` | `serialize_model`/`deserialize_model` already store a `LogisticRegression` as plain JSON (`coef`, `intercept`, `classes`). **Mirror this exactly — no pickle, no joblib.** |
| Label vocabulary | `image_extractor.py:40` `_VALID_TYPES` | The head's classes are exactly these 8 labels. See §1. |
| Rejection cache + reset | `record_image_rejection`, `clear_image_rejections`, `alexandria images --reset-rejections` | The re-evaluate path after a retrain already exists. |
| CLI command registry | `pka/cli/__init__.py::COMMANDS` | One new entry + one new module. |
| `data/` conventions | `cfg.data_dir` (`data/`, git-ignored) | Training folders and the model artifact both live here. |

So the work is: an embedding cache, a training/eval command, a predict path, and
a config-gated swap in `gate_image`.

## 1. The constraint that shapes everything: the gate's label is load-bearing

The obvious reading of "image gate" is a binary admit/reject. **It is not.**
`pka/ingestion/image_pipeline.py:344-352` documents that when the gate runs, its
label — not the main pass's — becomes:

- `images.image_type`,
- the `TagOrigin.INFERRED` overlay tag, and
- the selector for the per-type content prompt (`extract_image_content(...,
  image_type=gate_type)`).

So the head must be **multi-class over the same 8 `_VALID_TYPES`**, not binary:

```
book_cover · multiple_book_covers · bookshelf · slide · poster · notes · whiteboard · unknown
```

`unknown` is the reject class; any other prediction admits *and* carries its
label downstream. This falls out cleanly — `LogisticRegression.classes_` holds
the vocabulary, and `_normalize_type` already exists to keep folder names honest.

A binary gate would silently regress `images.image_type` to whatever the main
VLM pass returns, quietly undoing the optimisation that comment describes. Worth
stating in the code, not just here.

## 2. Training data on disk

```
data/image_gate_training/
    book_cover/            *.jpg *.png …
    multiple_book_covers/
    bookshelf/
    slide/
    poster/
    notes/
    whiteboard/
    unknown/
```

One folder per label, images dropped in directly, as requested. Rules:

- Folder name must be in `_VALID_TYPES` (run it through `_normalize_type`);
  an unrecognised folder is a hard error listing the valid names, not a silent
  skip — a typo'd folder would otherwise cost a training run.
- Path configurable via `cfg.image_gate_training_dir`, defaulting to
  `data_dir / "image_gate_training"`. Under `data/`, so already git-ignored.
- **`unknown/` is the class that decides gate quality and the one that will be
  under-populated.** It is "everything else in a photo library": screenshots,
  UI, people, landscapes, memes, receipts, blurry shots. If it is small or
  homogeneous the head will happily admit half the library. Say so in the CLI
  help and warn at train time when `unknown/` is under ~20% of the set.

### 2.1 Embedding cache

CLIP-embed each training image once and cache to
`data/image_gate_training/.embeddings.json` keyed by path + mtime + size.
Retraining after adding a folder then only embeds the new files. This is what
makes "trained or retrained from CLI" a fast loop rather than a full re-encode
every time — the encode is the entire cost of training.

### 2.2 Optional bootstrap from the archive

Images already ingested carry a VLM-assigned `images.image_type` *and* a stored
CLIP vector (`images.clip_vector_id` → the `alexandria_clip` collection). That is
a labelled training set at zero labelling cost and zero re-encoding cost —
offer it as `--from-archive` to merge in.

Be honest about what it is: **distillation of `moondream`.** It inherits the
VLM's mistakes and its ceiling is the VLM's accuracy. It is the fast route to a
working v1; the hand-curated folders are the route to something that *beats* the
VLM. Keep folders as the primary source and archive rows as an additive option.

## 3. Model

- **Backbone:** frozen CLIP ViT-B/32 image encoder. No fine-tuning, no gradients,
  no torch training loop — `embed_image` is called as-is.
- **Head:** `LogisticRegression(max_iter=1000, class_weight="balanced")`.
  Folder counts will be uneven, and `balanced` is what stops a large
  `book_cover/` from swamping `whiteboard/`. Inputs are already L2-normalised,
  which is what a linear head on CLIP features wants.
- **Not an MLP.** At the realistic scale here (hundreds to low thousands of
  images) a linear probe on CLIP features is the standard, and it stays
  JSON-serialisable via the existing helper. Revisit only if the eval says the
  classes are not linearly separable.
- **Artifact:** `data/models/image_gate.json` (`cfg.image_gate_model_path`) —
  same JSON payload as `tag_training`, plus `clip_model`, `trained_at`,
  `n_train`, per-class counts, and held-out metrics. Refuse to load a model whose
  stored `clip_model` differs from `cfg.clip_model`: the vectors would be from a
  different space and the predictions silently garbage.

## 4. CLI

`alexandria image-gate-train` — new `pka/cli/image_gate_train.py`, registered in
`COMMANDS`.

| Flag | Meaning |
|---|---|
| *(none)* | Embed (cached) → train → evaluate → write the model |
| `--dry-run` | Train and print the report, write nothing |
| `--from-archive` | Merge in already-ingested images and their stored CLIP vectors (§2.2) |
| `--test-size` | Held-out fraction, default 0.2, stratified |
| `--no-cache` | Ignore `.embeddings.json` and re-encode |
| `--report-only` | Load the existing model and evaluate it against the folders |

Evaluation output (printed, and stored in the artifact):

- per-class precision / recall / support,
- the confusion matrix,
- **the `unknown`-vs-rest boundary specifically** — that is the admit/reject
  decision, and a model with good mean accuracy can still be bad exactly there,
- the abstain rate at the configured threshold (§5).

Stratified split, fixed `random_state`, and a refusal to train when any class has
too few examples to split (say < 5) rather than reporting a meaningless score.

## 5. Wiring into the gate

`gate_image` gains a classifier branch behind `cfg.image_gate_classifier_enabled`
(**default off**), falling back to today's VLM path when disabled, when no model
file exists, or on abstain:

```
coverage check (unchanged)
  → classifier: predict_proba on the CLIP vector
      → max proba < cfg.image_gate_classifier_min_confidence  → abstain → VLM path
      → "unknown"                                             → reject
      → any other label                                       → admit, label flows downstream
```

**Abstain falls back to the VLM rather than guessing.** That is what makes the
rollout safe: a half-trained model degrades to current behaviour and costs a VLM
call, instead of poisoning the archive.

### 5.1 The asymmetry that sets the threshold

A false *reject* is written to `image_rejections` and is permanent until someone
runs `--reset-rejections` — the image silently never enters the archive. A false
*admit* costs one wasted VLM call on an image that then gets described anyway.
These are not equally bad. **Bias the threshold toward admitting**, and treat
`unknown` predictions as needing more confidence than the others.

The existing `strict=True` behaviour (a VLM outage raises rather than degrading
to `unknown`, which would cache a permanent rejection) exists for exactly this
reason. A local classifier has no outage mode, which removes that failure — but
introduces a new one: a *quietly bad model* mislabelling at full speed. The
confidence threshold and the default-off flag are the answer to it.

## 6. Does the coverage step still run first?

`gate_image` runs EasyOCR text-coverage first as "the cheap local pass", then the
VLM. With a CLIP head that ordering is worth re-checking rather than inheriting:
EasyOCR runs CRAFT detection over the image, while CLIP ViT-B/32 is a single
forward pass — plausibly the *cheaper* of the two, which would invert the
rationale for the current order.

**Measure before reordering.** Time both on ~50 real images from the library and
put the numbers in this file. Note that torch is CPU-only on this machine
(Pascal `sm_61`), so both run on CPU and the gap may be smaller than GPU
benchmarks suggest.

Keep the coverage gate in place either way for v1: it is an orthogonal filter
(*is there text at all*) and cheap insurance while the classifier is unproven.
Dropping or reordering it is a follow-up justified by measurements, not part of
this change.

## 7. Config

| Setting | Default | Notes |
|---|---|---|
| `image_gate_classifier_enabled` | `False` | Add to the boolean validator list at `config.py:439` |
| `image_gate_classifier_min_confidence` | `0.6` | Below → abstain → VLM |
| `image_gate_training_dir` | `data_dir / "image_gate_training"` | Property, like `chroma_dir` |
| `image_gate_model_path` | `data_dir / "models" / "image_gate.json"` | Property |

No outbound network path is added: CLIP loads from the local HF cache with
`HF_HUB_OFFLINE=1` (`providers/clip.py:28`), so `DESIGN.md` §1.1 is unaffected —
this change *removes* a call to a local Ollama, and adds none.

## 8. Tests

`tests/conftest.py` mocks CLIP and disables the gate, so tests must not touch
real images or a real encoder.

- **Train/serialise/reload round-trip** on synthetic separable vectors (no
  images, no CLIP): assert the reloaded head predicts identically.
- **`unknown` → reject, other labels → admit**, and the label is carried on
  `GateResult.image_type` — the §1 invariant, which is the one most likely to
  regress silently.
- **Abstain path**: a low-confidence prediction falls back to the VLM (assert the
  VLM provider *was* called); a confident one does not (assert it was not).
- **`clip_model` mismatch** between artifact and config refuses to load.
- **Folder loader**: an unrecognised folder name raises listing valid names; the
  embedding cache is reused on a second call (assert the encoder ran once).
- **CLI**: `--dry-run` writes no artifact. Note `pka/cli/*` is omitted from
  coverage, so keep the logic in an importable module and the CLI thin.

## 9. Success criteria

The VLM is the incumbent and this must be measured against it, on a **hand-labelled
held-out set** — not against the VLM's own labels, which is what `--from-archive`
trains on and would make the comparison circular.

- **Agreement with hand labels ≥ moondream's** on the same held-out set.
- **`unknown` recall specifically** — the reject decision, weighted by §5.1's
  asymmetry.
- **Latency per image** vs the VLM call it replaces (the actual point of this).
- If it loses on accuracy but wins hugely on latency, the abstain threshold is
  the dial: keep it high, let the classifier handle the confident majority, and
  let the VLM handle the rest. That hybrid is a legitimate end state, not a
  failure.

## 10. Phasing

1. Embedding cache + folder loader + train/eval CLI. **No pipeline change.**
   Prove the numbers first (§9).
2. Classifier branch in `gate_image` behind the default-off flag, with VLM
   fallback on abstain.
3. Measure on a real ingest run, tune the threshold, then consider flipping the
   default and revisiting §6.

## 11. Docs

- `DESIGN.md` §3.2 describes the gate as a VLM classification step — it becomes
  "a CLIP linear probe, with the VLM as fallback".
- `docs/ingestion-flows.md` **must** change in the same commit: the image graph's
  gate node is a purple/red outbound-ish VLM call today and becomes a local one.
  The `CLAUDE.md` sync rule names exactly this (re-gating a call, shared vs.
  source-specific).
- `docs/persisted-fields.md`: check whether the provenance of `images.image_type`
  needs a note — the column's writer changes, even though no column is added.
- `README.md` / `INSTALL.md`: the new CLI command and the training folder layout.
