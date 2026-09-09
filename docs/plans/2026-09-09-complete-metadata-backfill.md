# Complete Metadata Backfill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an auditable, source-targeted workflow that enriches every resolvable historical AudioSpider row before safely scaling collection.

**Architecture:** A new metadata-only CLI groups formal database rows by source and reuses source adapters for deterministic detail lookup. Storage performs non-destructive enrichment by URL or stable source ID; a structured audit distinguishes rich, partial, reference-only, not-provided, and unresolved records.

**Tech Stack:** Python 3.11, asyncio, aiohttp, BeautifulSoup, SQLite, unittest.

---

### Task 1: Metadata audit contract

**Files:**
- Modify: `storage.py`
- Create: `tests/test_metadata_backfill.py`

1. Write tests for source/status filtering and per-field coverage counts.
2. Run the focused tests and confirm failure.
3. Add read-only record selection and audit helpers.
4. Run the focused tests and confirm pass.

### Task 2: Targeted source enrichment

**Files:**
- Create: `metadata_backfill.py`
- Modify: `spiders/bilibili.py`
- Test: `tests/test_metadata_backfill.py`

1. Write tests for stable Bilibili part IDs, Ximalaya track IDs, Xiaoyuzhou podcast IDs, bulk RSS/LibriVox matching, and failure isolation.
2. Implement source handlers without downloading audio.
3. Ensure handlers return real matched records only and never synthesize rich metadata for unresolved rows.
4. Emit a JSON-safe report with before/after audits and unresolved IDs.

### Task 3: CLI, documentation, and release gate

**Files:**
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `docs/reference/cli.md`
- Modify: `docs/reference/background-metadata.md`
- Modify: `docs/guides/download-and-convert.md`
- Modify: `docs/README.md`

1. Document historical metadata backfill before background-file refresh.
2. Add safe examples for audit-only and per-source runs.
3. Run `bash scripts/test.sh`, `git diff --check`, and Markdown link validation.
4. Commit the implementation before touching the formal database.

### Task 4: Production backfill and scaled collection

**Files:**
- Create: `docs/reports/2026-09-09-complete-metadata-and-scale-validation.md`

1. Wait for the unique legacy downloader to exit; do not start a second retry.
2. Verify SQLite integrity and create an online backup.
3. Run the complete metadata backfill and inspect unresolved rows per source.
4. Refresh background files for all done physical media.
5. Collect the selected large target in bounded source batches; audit every batch.
6. Download all new pending rows with background mode `all` under the existing lease and disk limits.
7. Validate every sidecar and physical media file, write the dated report, run the release gate, and commit the report.
