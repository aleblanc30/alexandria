# Backlog

Deferred work items. Each entry: what, why deferred, and rough shape of the eventual implementation.

## YouTube connector

### Transcript-based Phase-2 content extraction

**What:** Enrich YouTube video documents with their caption transcript (auto-generated
or manual) as embeddable full text, instead of only title + channel + description.

**Why deferred:** The initial YouTube connector ingests **metadata only** (title,
channel, description via the YouTube Data API v3). Transcripts add a new dependency
(`youtube-transcript-api`) and outbound calls to `youtube.com` per video, plus
handling for videos with captions disabled. Shipping metadata-only first keeps the
connector lean and lets the Data API path stabilise before layering content fetch on top.

**Rough shape when picked up:**
- Add `youtube-transcript-api` to `pyproject.toml` dependencies.
- New `pka/ingestion/youtube_transcript.py`: fetch transcript for a video ID,
  concatenate cues into text, handle `TranscriptsDisabled` / `NoTranscriptFound`
  gracefully (fall back to metadata-only, do not mark the doc unfetchable).
- Turn the metadata-only ingest into a true two-phase flow: Phase 2 fetches the
  transcript, builds `title + channel + description + transcript` via a helper
  (mirror `pka/ingestion/preprint_text.build_preprint_text`), and embeds via
  `ingest_text_block`. Offset chunk indices past Phase-1 chunks with
  `existing_chunk_count()` so both passes coexist in one document (see DESIGN §3).
- Prefer manually-authored captions over auto-generated; pick language by config
  (`ALEXANDRIA_YOUTUBE_TRANSCRIPT_LANGS`, default `["en"]`).
- Mock `youtube-transcript-api` in `tests/conftest.py`; add transcript cases to
  `tests/test_connector_youtube.py`.

### Watch Later ingestion (Data API can't reach it)

**What:** Include the user's **Watch Later** queue among the ingested saved videos.

**Why deferred / blocked:** The YouTube Data API v3 no longer exposes the special
`WL` playlist — `channels.list → contentDetails.relatedPlaylists.watchLater` was
removed years ago and no OAuth scope brings it back. So the current connector
(Liked videos + owned playlists) structurally cannot see Watch Later. Any fix
means reaching WL through a channel other than the official API.

**Options, ranked:**
1. **Convert WL to a normal playlist (durable, no code).** A regular playlist
   (e.g. "To Watch") *is* API-visible, so the connector already ingests it. Only
   helps going forward; no one-click WL→playlist copy exists in YouTube.
2. **yt-dlp with browser cookies (recommended bridge).** `yt-dlp` authenticates as
   the logged-in web client (InnerTube), which still serves WL:
   `yt-dlp --flat-playlist --dump-single-json --cookies-from-browser firefox
   "https://www.youtube.com/playlist?list=WL"`. Runs fully locally (fits
   local-first better than OAuth). Recommended shape: use yt-dlp **only to get WL
   video IDs**, then reuse the existing Data API `videos.list` hydration (public,
   per-ID, no OAuth) for title/channel/description/tags — confining the fragile
   part to one small loader. Add a `load_watch_later_ids_via_ytdlp()` behind a
   flag (`ALEXANDRIA_YOUTUBE_WATCH_LATER=1` + a cookies source), merge into
   `load_saved_videos()` as a playlist named `Watch Later`, add `yt-dlp` as an
   optional extra, and mock it in tests.
3. **Browser automation (last resort).** Scrape `youtube.com/playlist?list=WL`
   from a logged-in session. Strictly worse than yt-dlp; no upside.

**Caveats to document (in the DESIGN §2.1 cloud-exception note):** yt-dlp is not an
official API — InnerTube changes can break it, heavy scraping risks account
rate-limiting, and `--cookies-from-browser` reads the browser cookie store
(local but sensitive). Flat mode gives no reliable added-date ordering.

**Not a fix:** Google Takeout — Watch Later has historically been excluded from
Takeout's YouTube playlist CSVs; verify before relying on it.
