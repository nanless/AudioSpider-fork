# Audio Background Metadata Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Save public source context, descriptions, covers, chapters, transcripts, and public-domain source text beside every downloaded audio when available.

**Architecture:** Add queryable common metadata columns plus versioned source JSON to `AudioRecord` and SQLite. Source adapters populate a normalized metadata envelope; a best-effort background asset downloader safely writes bounded auxiliary files and updates the sidecar without changing successful audio status.

**Tech Stack:** Python 3.11, asyncio, aiohttp, BeautifulSoup/lxml, SQLite, unittest, ffmpeg/ffprobe.

---

### Task 1: Metadata schema and migration

**Files:** Modify `storage.py`; test `tests/test_storage.py`.

1. Add failing tests for old-database migration and rich metadata upsert.
2. Add AudioRecord fields and additive SQLite columns.
3. Implement enrichment of existing URL rows without erasing non-empty values.
4. Run storage tests.

### Task 2: Normalization and safe asset persistence

**Files:** Create `background.py`; modify `config.py`; test `tests/test_background.py`.

1. Test HTML-to-text, URL redaction, filename/MIME mapping, byte limits and private-network blocking.
2. Implement versioned metadata helpers and bounded asset downloads through `safe_get`.
3. Save description, cover, transcripts, chapters and source text adjacent to audio.
4. Return structured asset results without raising audio-fatal errors.

### Task 3: RSS metadata

**Files:** Create `rss_metadata.py`; modify `spiders/podcast_rss.py` and `discover.py`; test `tests/test_rss_metadata.py` and `tests/test_discover.py`.

1. Add fixture coverage for RSS description/content, author, webpage, image, persons, license, transcript and chapters.
2. Normalize channel/item data and declared assets.
3. Attach it in both fixed RSS and discovery parsers.

### Task 4: Platform adapters

**Files:** Modify all non-RSS files under `spiders/`; add `tests/test_spider_metadata.py`.

1. Parse Xiaoyuzhou `__NEXT_DATA__` with regex fallback.
2. Request LibriVox extended/coverart fields.
3. Preserve Ximalaya baseInfo public context.
4. Use B站 view/player data for description, owner, cover, pages and public subtitles.
5. Verify missing fields remain empty rather than fabricated.

### Task 5: Download and backfill commands

**Files:** Modify `downloader.py` and `main.py`; test `tests/test_downloader.py`.

1. Add background mode to regular downloads.
2. Extend sidecar with common/source metadata and asset results.
3. Add a background action for existing done files.
4. Ensure asset failures do not change audio done.
5. Ensure signed query parameters are absent from sidecars and logs.

### Task 6: Documentation and skill

**Files:** Modify `README.md`, `CHANGELOG.md`, `.env.example` and relevant `docs/` references; update installed `audiospider-project` skill.

1. Document availability versus guaranteed fields.
2. Document new CLI, layout, limits, provenance and ASR boundary.
3. Run Markdown link validation and skill validation.

### Task 7: Server verification and rollout

1. Wait for the current single retry downloader to exit.
2. Run the complete offline suite and doctor.
3. Back up SQLite, deploy code, and rerun bounded source probes into the formal DB to enrich rows.
4. Run background backfill for physical done files.
5. Validate asset counts, JSON parsing, ffprobe, database integrity and Git diff.
6. Commit implementation and a dated validation report; do not push without authorization.
