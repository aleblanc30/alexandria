# Migration runbook: upgrading the production archive

The steps that bring the production instance (port 8420) up to the current
branch, in the order that avoids doing expensive work twice. Everything here
runs against the real library, so step 1 comes first and is not optional.

What changed, and what each change needs from an existing archive:

| Change | Needs | Step |
|---|---|---|
| New dependencies (`sentence-transformers`, `semantic-text-splitter`, `pysbd`) | `pip install .` | 2 |
| `schema_migrations`, `document_texts` columns, the Zotero `pass="metadata"` tag, FTS5 keyword indexes | `alexandria init` (the FTS5 build takes minutes) | 4 |
| Retired chunk settings | remove them from `.env` | 3 |
| Duplicate Calibre full-text and summary chunks from earlier runs | `alexandria purge duplicate_chunks` | 5 |
| Embedding model `all-MiniLM-L6-v2` → `multilingual-e5-small` | `alexandria reembed` | 6 |
| Token-sized chunker | `alexandria rechunk` | 7 |
| Zotero PDF full text; Zotero collections read with their parents | one `alexandria zotero` sync | 8 |
| Collection tags for documents already archived | `alexandria collection-tags` | 9 |
| Duplicate and equivalent tags | `alexandria dedupe-tags` (spellings fold on their own) | 10 |
| The same work saved twice | `alexandria dedupe scan` | 11 |
| Calibre books whose retained text is only the first 20 pages | optional re-extraction | 12 |
| Clustering runs from the old model stop taking new documents | a new run, accepted | 13 |

The fetch-queue fix, the module splits and the layering contract need
nothing.

## 1. Stop the server and back up the library

Stop the instance on 8420. Ending the scheduled task leaves uvicorn holding
the port (INSTALL.md §6), so check that nothing listens on it afterwards.

Copy the whole data directory (`ALEXANDRIA_DATA_DIR`, default `data/`): it
holds `archive.db` and `chroma/`, and the two are only restorable as a pair.
`alexandria reembed` deletes and rebuilds the Chroma collection, so this copy
is the way back if it fails partway.

## 2. Update the code and install

```powershell
git pull            # or check out the release tag
pip install .
cd frontend; npm ci; npm run build; cd ..
```

`scripts\upgrade.ps1 <tag>` does steps 1, 2 and 4 in one go once a tag exists,
but it restarts the server at the end: stop it again before step 5.
`torch` was already a dependency, so the install adds little beyond the three
packages above.

## 3. Clean `.env`

Remove any of these lines; they are no longer settings. The app starts anyway
and logs a warning for each, but a stale line is misleading.

```
ALEXANDRIA_CHUNK_SENTENCES
ALEXANDRIA_CHUNK_OVERLAP
ALEXANDRIA_MAX_SENTENCE_CHARS
```

The new ones default to `ALEXANDRIA_CHUNK_TOKENS=256` and
`ALEXANDRIA_CHUNK_OVERLAP_TOKENS=32`. Leave `ALEXANDRIA_EMBEDDING_MODEL` unset
for `intfloat/multilingual-e5-small`; set it to `all-MiniLM-L6-v2` only to
stay on the old model, and then skip step 6.

## 4. Migrate the database

```powershell
alexandria init
```

Runs every migration step once and records it. On an archive that predates
`schema_migrations` every step runs (each checks before it alters). The FTS5
chunk index is built here, over every stored chunk: minutes on a large
archive, and only this once.

## 5. Remove duplicate Calibre chunks

```powershell
alexandria purge duplicate_chunks --dry-run
alexandria purge duplicate_chunks
```

Before step 6, so the copies are not re-embedded. Only documents whose repeated
runs are exact copies are touched; the first copy stays.

## 6. Move to the new embedding model

Smoke-test the model first. The first load downloads it from the Hugging Face
Hub (a few hundred MB), once:

```powershell
python -c "from pka.storage.embedding import get_embedder as g; e=g('intfloat/multilingual-e5-small'); print(len(e.embed_query('bonjour')), e.max_chunk_tokens)"
```

Expect `384` and a number a few under 512. The prefixes and the 512-token
limit were set from the model card without a live check, so stop here if
either differs.

```powershell
alexandria reembed
```

Re-embeds every chunk into a rebuilt collection (keeping each chunk's
metadata), recomputes every document vector, and retrains and re-applies the
learned-tag models. Time scales with the chunk count; on CPU it is the long
step. Read the end of its log:

- `tag model(s) retrained`: the count should match your trained sessions.
- `Could not retrain, model removed: …` names any tag whose labels no longer
  fit a model. Resume those sessions in the UI and label a few more.

## 7. Re-cut retained text with the new chunker

```powershell
alexandria rechunk --dry-run
alexandria rechunk
```

After step 6, so the new chunks are sized for e5's tokenizer (sizing uses the
model the index records) and embedded once, with e5. This re-embeds body chunks
a second time; the alternative order would size every chunk for MiniLM's
tokenizer instead.

It covers documents with retained body text: Firefox and Reddit link posts
fetched since retention shipped, and Calibre books. Anything older keeps its
old chunks, which still work. The final log line counts books skipped because
only their first pages are retained (step 12).

## 8. Pull in Zotero PDF full text

```powershell
alexandria zotero
```

The sync now ends with a full-text pass that reads each attached PDF, retains
its text and embeds it by page group. Items already read, or found to be scans,
are skipped, so a later sync adds nothing. After steps 6 and 7, so these chunks
are cut and embedded once, the new way. A scanned PDF is marked
`no_text_layer` and waits on the OCR item in `TODO.md`.

The same sync rewrites every archived item's collections as paths with their
parents (`Thesis/Chapter 2`, where only `Chapter 2` was stored), and tags the
items with them.

## 9. Tag documents with their collections

```powershell
alexandria collection-tags --dry-run
alexandria collection-tags
```

Derives `collection` tags from the stored collections, for Firefox bookmarks
(whose folders are written only when first archived) and anything step 8 did
not touch. SQLite only, seconds. After step 8, so Zotero tags come from the
full paths.

Firefox folders are the noisy part. Read the dry run's two lists: the most used
tags per source, and the tags already left out for sitting on more than 1000
documents. Then tune in `.env` and dry-run again until the list reads like
topics:

```
ALEXANDRIA_COLLECTION_TAG_EXCLUDE=Imported,Misc,Other Bookmarks
ALEXANDRIA_COLLECTION_TAG_MAX_DOCUMENTS=300
ALEXANDRIA_COLLECTION_TAG_MAX_DEPTH=3
```

An excluded name drops only that folder; its subfolders are still tagged.
`ALEXANDRIA_COLLECTION_TAGS_ENABLED=false` turns the whole feature off, and
the next run removes the tags. Re-running after any change converges.

## 10. Review duplicate tags

Spellings of one tag (`Machine Learning`, `machine-learning`, `Économie` /
`economie`) are already one tag from step 2 on; nothing to run. For the rest:

```powershell
alexandria dedupe-tags report            # what the spelling fold already merges
alexandria dedupe-tags scan --dry-run    # proposals, with similarity scores
alexandria dedupe-tags scan
```

Then review on the Tags page (*Duplicate tags*), or with `alexandria
dedupe-tags list` / `accept <id>` / `reject <id>`. After step 6, because the
semantic proposals use the model the chunk index records: before it, that is
the English-only MiniLM. The similarity cut-off (`ALEXANDRIA_TAG_DEDUP_SIMILARITY`,
0.92) was set without your vocabulary; if the dry run lists pairs you would
keep apart near the bottom of its scores, raise it, and if it finds almost
nothing, lower it to 0.88 and look again. Declining a pair is remembered, so
re-scanning never re-asks.

## 11. Link duplicate documents

```powershell
alexandria dedupe scan --dry-run
alexandria dedupe scan
```

The dry run lists the pairs it would link (same DOI, arXiv id, ISBN or URL)
and the near duplicates it would propose. Linking hides nothing permanently:
the duplicate's card folds into its canonical, and *Unlink* on the Ingestion
page restores it. Near duplicates wait there for *Link* / *Keep apart*. After
step 6, because near duplicates compare the document vectors the re-embed
recomputes, and before step 13, so the new clustering run leaves duplicates
out. If the near-duplicate list pairs different papers of one series, raise
`ALEXANDRIA_DEDUPE_SIMILARITY` above 0.97; linking never happens for those
without you.

## 12. (Optional) Re-extract truncated Calibre books

Books retain only their first 20 pages, so `rechunk` skips them, and their
full text keeps the old chunks. Search still works on those. To re-cut them,
re-extract the whole Calibre library from the files, minutes per book:

```powershell
alexandria purge fetched_text --source calibre --dry-run
alexandria purge fetched_text --source calibre
alexandria calibre --fulltext
alexandria purge duplicate_chunks --dry-run
```

The purge is source-wide: it also drops the Calibre chunks step 7 already
re-cut. With `book_summary_enabled` on, the re-extraction appends a second copy
of each cached summary chunk, which the last command should report; run it
without `--dry-run` if it does.

## 13. Re-cluster

The accepted clustering run was built from MiniLM vectors, and new documents
are no longer assigned to it. Run clustering again, from the Clusters page or:

```powershell
alexandria clustering
```

Review the run and accept it (`--accept` skips the review).

## 14. Start the server and check

Start the scheduled task, then:

- `server.log` has no "chunk index was built with all-MiniLM-L6-v2" warning.
- A French or Spanish query finds documents in that language.
- Keyword (`fulltext`) search finds a phrase from a PDF body, not only titles.
- The Settings page lists the Embedding, Chunking and Tags groups with the new
  fields.
- The Browse sidebar has a Collections group; picking a parent folder also
  shows its subfolders' documents.

If step 6 or 7 failed partway, stop the server, restore the step 1 copy and
rerun from the failed step.
