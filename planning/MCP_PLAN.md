# MCP server for Alexandria

**Status:** proposed, not implemented.
**Closes:** `planning/TODO.md` → *MCP → MCP server for document search*.
**Touches:** new `pka/mcp/`, new `pka/cli/mcp.py`, `pka/cli/__init__.py`,
`pka/api/routers/documents.py`, `pka/api/schemas/documents.py`, `pka/config.py`,
`pyproject.toml`, `DESIGN.md`, `README.md`, `tests/test_mcp_server.py`.

## Goal

Let an MCP client — Claude Code, Claude Desktop, any other — search the archive
and read the text behind a hit, so a conversation can be grounded in the user's
own library and cite back to it. Read-only.

The archive is worth exactly as much to a model as its *passages* are. A search
result that returns only titles and 280-char card summaries makes the client
guess; one that can pull the indexed chunk text lets it quote. That distinction
drives most of what follows, including the one backend endpoint this needs.

## Why Zotero and Firefox first

Not an arbitrary slice — those two are the sources whose documents have both
**text worth retrieving** and **a stable external identity to cite**:

- **Zotero** is the scholarly corpus: title, abstract, authors, collections, and
  a PDF sitting on disk that a `zotero://` link can open in the app.
- **Firefox** is the web corpus, and the only source with a real phase-2 fetch —
  extracted article body text, Wayback snapshots, and the arXiv / bioRxiv /
  Wikipedia handlers that turn a bookmark into a paper.

The others are deferred for reasons, not by oversight. Calibre is full books
(hundreds of body chunks per document — a result-shaping problem of its own).
Images retrieve through inferred text or CLIP, which is a different query model
(`DESIGN.md` §3.3). Reddit and YouTube are thinner and noisier.

**The mechanism is a default, not a wall.** `sources` on the search tool defaults
to `["zotero", "firefox"]` and accepts the whole `Source` enum. Phase 2 is
changing a default and a tool description, not rewriting a tool.

## The load-bearing decision: how the MCP server reaches the archive

**Proposed: a stdio MCP server that is an HTTP client of the running Alexandria
API on loopback.** Not a second process opening the archive directly.

The reason is Chroma. `pka/storage/vector_store.py` builds a
`chromadb.PersistentClient` over `data/chroma`, which is a single-process store —
the module docstring already documents how badly it goes when *one* process
races itself on client creation. A second OS process holding the same persist
directory while `alexandria dev` or the installed server is up is a category
worse than that race, and search is the entire point of this feature. The API
process is already the single owner of that store; the MCP server should stay a
client of it.

Two further arguments point the same way, and would carry even without Chroma:

- **Startup cost.** An MCP client spawns a stdio server and waits. Importing
  `pka.db.queries` → `pka.storage.vector_store` → `chromadb` pulls in torch and
  the sentence-transformers stack; the tool layer instead needs `mcp` + `httpx`,
  both small.
- **No second copy of the query logic.** `/search`'s merge of semantic hits,
  fulltext fallback, CLIP folding, and the browse-filter passes is ~180 lines of
  ordering decisions. Reimplementing it against the library for the MCP path
  gives two search behaviours that drift.

**This assumption is the thing to verify first.** If multi-process
`PersistentClient` reads turn out to be safe in the installed configuration, a
local backend becomes a legitimate option and the argument narrows to startup
cost. Check before building, not after.

### Alternatives considered

**Mount MCP inside the FastAPI app** (Streamable HTTP at `/mcp`, one process, no
extra lifecycle). Genuinely attractive and probably the eventual second
transport — it removes the proxy hop entirely. Not first, because stdio is the
transport every client supports without configuration, and because a stdio
server can fail with *"Alexandria is not running — start it and retry"*, which
is the actual failure mode a user will hit. The tool handlers should be written
so this is an added entry point later, not a rewrite: keep tool definitions and
handlers in `pka/mcp/tools.py`, transport in `pka/mcp/server.py`.

**Direct library access from the stdio server.** Rejected above. Worth
re-opening only if the Chroma finding says otherwise.

**Expose the whole REST API through a generic proxy tool.** One `call_api` tool
taking a path. Cheap to write, bad to use: the model gets no schema, no
defaulting to Zotero+Firefox, no result shaping, and full reach over the write
endpoints. The value of an MCP layer is the curation.

## Tool surface (v1)

Five tools. Small on purpose — every tool description competes for the client's
context, and a model chooses better among five clear tools than fifteen.

| Tool | Backs onto | Purpose |
|---|---|---|
| `search_library` | `POST /search` | Semantic / hybrid / fulltext search, source- and tag-filtered |
| `get_document` | `GET /documents/{id}` | Full record for one hit: tags, collections, enrichment, links |
| `get_passages` | **new** `GET /documents/{id}/chunks` | The indexed chunk text, for quoting and citation |
| `list_documents` | `GET /documents` | Browse by tag / collection with no query |
| `list_tags` | `GET /tags` | The tag vocabulary, so filters can be constructed rather than guessed |

`search_library` params: `query`, `sources` (default `["zotero","firefox"]`),
`mode` (`semantic` default), `limit` (default 10, max 50), `source_tags`,
`general_tags`, `date_from` / `date_to`. These map straight onto `SearchRequest`;
no new API shape.

**Read-only, deliberately.** No tag patching, no `/ingestion/sync`, no clustering
runs. An MCP server is an input surface where a model picks the calls, and some
of the text it reads back is arbitrary fetched web content — so the worst case
should be a wrong answer, never a mutated archive or a sync that hits the
network. This also keeps the server inside `CLAUDE.md`'s "do not run real
ingestion" boundary by construction.

## The one backend addition: `GET /documents/{doc_id}/chunks`

Nothing today serves chunk *text* over HTTP. `DocumentDetail` carries
`chunks_count` and the enrichment chunks, but not the body — the frontend never
needed it. `get_passages` does.

```
GET /documents/{doc_id}/chunks?offset=0&limit=20&chunk_pass=fulltext
→ { total, chunks: [{ chunk_index, text, chunk_pass, page_start, page_end }] }
```

- Plain `def`, not `async def` — sync SQLAlchemy in an `async def` handler is
  what commit `9ae82d4` removed from every router, and a new one must not
  reintroduce it.
- `chunk_pass` filter matters: it separates `metadata` from `fulltext` from
  `summary` / `external_synopsis` (`DESIGN.md` §3.2), which is exactly the
  distinction a caller wants when asking "what does the *document* say" versus
  "what did we generate about it".
- `page_start` / `page_end` come along because Calibre and the PDF route carry
  them, and a citable page range is the difference between a quote and a claim.
  Firefox's fetch route leaves them null by design — the tool must not imply
  otherwise.
- Add to the vite proxy? No — `/documents` is already proxied, and this is a
  sub-path of it.

## Per-source facts that shape the tool descriptions

These are the things a model will get wrong unless the tool tells it, and each
one is a property of the current pipeline, not a guess:

**Zotero indexes title + abstract only.** `item.pdf_path` is recorded and never
read (`planning/TODO.md`; `DESIGN.md` §3.2 table). So `get_passages` on a Zotero
paper returns the abstract, not the paper. Saying so in the tool description is
the difference between a model that hedges correctly and one that claims to have
read a PDF it has not seen. When the pending "Ingest Zotero PDF attachments"
TODO lands, this note changes and `chunk_pass=fulltext` starts returning body.

**Zotero authors, DOI, and year are not columns** — *until
`planning/DOCUMENT_METADATA_PLAN.md` lands.* `_zotero_document_kwargs` persists
source, id, title, url, date, fetch status, attachment key, item type, and that
is all; authors and DOI survive only inside `zotero_embed_text`'s blob
(`"Title\n\nby A, B\n\nAbstract"`), i.e. inside the metadata chunk. So today a
bibliographic answer must come from `get_passages`, not from `get_document`.

Whichever ships first, `get_passages` stays in v1 — it is what lets the client
quote abstract and article text at all. But if the metadata plan lands first,
`get_document` should return `doi` / `year` / `authors` directly and this note
becomes one line rather than a caveat in a tool description.

**Firefox `fetch_status` is load-bearing.** A bookmark that has not been fetched
(or was `unfetchable`, or is `no_text_layer`) has only title + card summary
embedded. A result that hides this makes "I found nothing in the article" and
"there is no article text" indistinguishable. `fetch_status` therefore belongs
in the *search result* shape, not just in detail.

**Deep links.** `zotero://open-pdf/library/items/<attachment_key>`, falling back
to `zotero://select/library/items/<source_id>` — logic that currently exists only
in `frontend/src/lib/zotero.ts`. The Python copy is four lines; put it in
`pka/mcp/links.py` with a comment naming the TypeScript original as its twin, and
accept the duplication rather than building a shared codegen for four lines.
Firefox's equivalent is `archive_url` when the fetch fell back to Wayback.

## Result shaping and the token budget

This is where an MCP layer earns its keep over a raw proxy. `DocumentOut` with
every tag, cluster label and overlay is a lot of tokens per hit, and ten hits of
it crowds out the answer.

- Search results project to: `id`, `source`, `title`, `url`, `description`
  (truncated ~280 chars), `similarity`, `fetch_status`, `source_tags`, plus the
  source-specific link. Cluster fields, overlay confidences and enrichment
  provenance stay in `get_document`.
- `get_passages` takes a total character budget (default ~6000) and truncates at
  a chunk boundary, reporting `returned`/`total` so the model knows to page
  rather than assuming it has everything. Silent truncation is the failure mode
  to avoid — a model that thinks it read the whole document will say so.

## Untrusted content

`get_passages` on a Firefox document returns text fetched from the open web.
That text reaches the client model as a tool result. Frame it as data: a short
provenance header per passage (source, document id, title) and no formatting
that could read as an instruction block. Worth a line in the tool description
too — the archive is the user's, but its *contents* are not all authored by them.

## Network policy position (`DESIGN.md` §1.1)

The server itself opens no outbound path: it talks to `127.0.0.1:8420` and
nothing else. Bind loopback only; no new flag is needed under §1.1 on that count.

But there is a genuine consideration §1.1's table does not currently cover: the
MCP *client* may be a hosted model, and whatever the tools return — document
text, passages, titles — goes to that client's provider. That is the same
exposure category as `chat_provider` pointed at OpenRouter ("document content"),
except the choice is made by which client the user connects rather than by an
Alexandria setting. §1.1 should gain a row saying so plainly. It is not a reason
to gate the feature; it is a reason not to let a reader conclude that a
local-first archive stays local when they attach a cloud client to it.

## Config, packaging, CLI

- **Settings** (`pka/config.py`): `mcp_api_url: str = "http://127.0.0.1:8420"`
  and `mcp_max_passage_chars: int = 6000`. Two, not a section — resist growing a
  config surface for a proxy.
- **Dependency**: the official `mcp` SDK as an optional extra, mirroring the
  `youtube` extra — core install unchanged, lazy-imported inside the entry point
  so `pka.mcp.tools` imports and unit-tests without it.

  ```toml
  mcp = ["mcp>=1.2"]
  ```
- **CLI**: `alexandria mcp` → `pka/cli/mcp.py`, registered in `COMMANDS`.
  Client wiring is then one line:

  ```bash
  claude mcp add alexandria -- alexandria mcp
  ```
- **Module naming**: `pka/mcp/` shadows nothing — `from mcp.server.fastmcp import
  FastMCP` inside `pka/mcp/server.py` resolves to the top-level SDK under
  absolute imports. It still reads confusingly; one comment at the top of
  `pka/mcp/__init__.py` costs less than the rename.

## Testing

Coverage matters here in a specific way: `pka/cli/*` is omitted from coverage but
**`pka/mcp/*` is not**, so the tool layer counts against `fail_under = 85`. Put
the logic in `pka/mcp/tools.py` and keep `pka/cli/mcp.py` a genuine three-line
shim.

`tests/test_mcp_server.py`, driving the handlers over an `httpx.MockTransport` —
no live API, no network, consistent with the conftest rule:

- `sources` defaults to `["zotero","firefox"]` and an explicit `sources` wins.
- Result projection: fields present, `description` truncated, `fetch_status`
  carried through, cluster/overlay noise dropped.
- `get_passages` respects the character budget, truncates on a chunk boundary,
  and reports `returned` < `total` rather than trimming silently.
- Zotero link falls back from attachment key to `zotero://select`; Firefox
  surfaces `archive_url` when present.
- API unreachable → one actionable error naming the URL, not a bare
  `ConnectError`.

Plus a router test in `tests/test_api.py` for `/documents/{id}/chunks`: paging,
`chunk_pass` filter, 404 on a missing document, page ranges present for a chunk
that has them and null for one that does not.

## Doc sync

- **`docs/ingestion-flows.md` needs no redraw.** Stating this explicitly because
  `CLAUDE.md` makes it a same-commit rule and the next reader will check: this
  change adds no source, alters no phase shape, moves nothing across the
  shared/source-specific line, adds no outbound call, and does not touch the
  shared tail. It is a read path over an already-built index.
- **`DESIGN.md`**: a new section for the MCP surface (the tools, the read-only
  boundary, the HTTP-client architecture and why), plus the §1.1 row above.
- **`README.md`**: the extra, the `claude mcp add` line, and the prerequisite
  that the API must be running.
- **`planning/TODO.md`**: tick the MCP entry, or point it here.

## Phasing

1. `GET /documents/{id}/chunks` + its router test. Independently useful, and it
   is the piece the frontend may eventually want too.
2. `pka/mcp/tools.py` — the five handlers over an injected `httpx.Client`, with
   projection and budgeting. This is where the tests live.
3. `pka/mcp/server.py` + `pka/cli/mcp.py` — FastMCP registration and stdio.
4. Docs, extra, `.env.example` if the two settings warrant it.

Deferred, in order of likely value: the remaining sources (Calibre first, and it
needs a result-shaping answer for book-length chunk sets); the `/mcp` Streamable
HTTP mount for clients that prefer it; a prompt or resource exposing the tag
vocabulary so the model does not have to call `list_tags` to start.

## Open questions

- Does multi-process `PersistentClient` read access actually break? The answer
  either confirms the architecture above or reopens the local-backend option.
- Should `search_library` default to `mode="hybrid"` rather than `semantic`? The
  API defaults to semantic with a fulltext fallback only when it returns nothing;
  a model searching for a remembered title is well served by hybrid, and badly
  served by a semantic miss. Worth measuring against the real archive before
  fixing the default.
- Is `list_documents` earning its slot in v1, or does `search_library` with an
  empty query plus tag filters cover it? Fewer tools is better if it does.
