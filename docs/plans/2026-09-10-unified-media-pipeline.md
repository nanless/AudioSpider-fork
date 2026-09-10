# AudioSpider Unified Media Pipeline Implementation Plan

> **Status:** Implemented on 2026-09-10. This file is retained as the execution record; use the
> current source and `--help` for exact behavior.

**Goal:** Make `collect.py -> audiospider.db -> main.py -> downloads/<source>/<category>/` the only formal orchestration path for ordinary audio, Bilibili video bundles, and complete YouTube parent videos.

**Architecture:** Extend the existing SQLite queue with an artifact discriminator, immutable `job_key`, bundle path and versioned spec. Keep task claiming, leases, retry, filtering, and reporting in `main.py`/`Storage`, then route claimed jobs to an audio handler or a video-bundle handler. Reuse the proven platform parsing and audit logic from the standalone dataset modules without keeping those CLIs as formal entry points.

**Tech Stack:** Python 3.11, SQLite/WAL, aiohttp, yt-dlp, ffmpeg/ffprobe, JSON sidecars, SHA-256, unittest.

---

The delivered implementation uses `audio_urls.artifact_kind`/`bundle_path`/`job_key`,
`spiders.youtube.YoutubeSpider`, Bilibili `video_bundle` records, `media_artifacts.py` dispatch from
the shared Downloader, `main.py --source` plus `--artifact-kind` filtering, and
`scripts/migrate_video_bundles.py` for default dry-run or explicit `--apply` migration. No separate
video sources default to `video_bundle`; legacy audio remains explicitly selectable.

The task sequence below records the intended TDD decomposition; it is not the current operator
runbook. The final implementation consolidated common video dispatch in `media_artifacts.py` and
extended existing tests instead of creating every provisional filename named below.

### Task 1: Lock the unified job contract with failing tests

**Files:**

- Modify: `tests/test_storage.py`
- Create: `tests/test_media_jobs.py`
- Modify: `storage.py`

**Step 1: Write failing migration tests**

Create an old-schema temporary SQLite database, initialize `Storage`, and assert every legacy row becomes logically equivalent to:

```python
{
    "artifact_kind": "audio",
    "artifact_spec_json": "{}",
}
```

Assert no existing status, URL, source ID, local path, hash, or lease changes.

**Step 2: Write failing validation tests**

Cover only the initial enum:

```python
ALLOWED_ARTIFACT_KINDS = {"audio", "video_bundle"}
```

Reject invalid JSON, credentials, signed URL query values and unknown kinds in a persisted artifact spec.

**Step 3: Run the focused tests**

Run:

```bash
python -m unittest tests.test_storage tests.test_media_jobs -v
```

Expected: new tests fail because the fields and validation do not exist.

**Step 4: Implement the minimal additive schema**

Add columns with backward-compatible defaults. Do not rename or drop `audio_urls` in this task. Add an index that supports status + artifact kind claims.

**Step 5: Re-run focused tests**

Expected: pass with no modification to a fixture representing the old production rows except additive defaults.

**Step 6: Commit the slice**

```bash
git add storage.py tests/test_storage.py tests/test_media_jobs.py
git commit -m "feat: add unified media job contract"
```

### Task 2: Add artifact-aware records and deterministic identities

**Files:**

- Modify: `storage.py`
- Modify: `spiders/base.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_spider_metadata.py`

**Step 1: Write failing tests**

Assert:

- ordinary `AudioRecord` defaults to `audio`;
- Bilibili/YouTube specs reject signed URLs and secrets;
- logical identity uses source, stable source ID, artifact kind and source revision;
- changing a temporary media URL does not create a new video job;
- identical stable jobs are idempotent.

**Step 2: Run the focused tests**

Expected: fail at missing fields/identity helpers.

**Step 3: Add the smallest shared record abstraction**

Preserve `AudioRecord` compatibility. Introduce a neutral `MediaJob` representation or an equivalent conversion helper without forcing every spider to rewrite at once.

**Step 4: Re-run tests**

Expected: pass.

**Step 5: Commit**

```bash
git add storage.py spiders/base.py tests/test_storage.py tests/test_spider_metadata.py
git commit -m "feat: normalize media job identities"
```

### Task 3: Extract reusable video-bundle primitives

**Files:**

- Create: `media_bundle.py`
- Modify: `bilibili_dataset.py`
- Modify: `youtube_dataset.py`
- Create: `tests/test_media_bundle.py`
- Modify: `tests/test_bilibili_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

**Step 1: Write failing common lifecycle tests**

Test deterministic staging, file records, SHA-256 closure, atomic promotion, stale staging detection, MP4 video+audio validation, 16 kHz mono PCM16 WAV validation and sidecar envelope validation.

**Step 2: Run tests and confirm failure**

```bash
python -m unittest tests.test_media_bundle -v
```

**Step 3: Move only platform-neutral code**

Keep Bilibili CID/DASH/player behavior in Bilibili code and yt-dlp caption dictionaries in YouTube code. `media_bundle.py` owns lifecycle and common validation, not platform interpretation.

**Step 4: Re-run all three focused suites**

```bash
python -m unittest \
  tests.test_media_bundle \
  tests.test_bilibili_dataset \
  tests.test_youtube_dataset -v
```

Expected: pass with current standalone output contracts unchanged.

**Step 5: Commit**

```bash
git add media_bundle.py bilibili_dataset.py youtube_dataset.py \
  tests/test_media_bundle.py tests/test_bilibili_dataset.py tests/test_youtube_dataset.py
git commit -m "refactor: share video bundle lifecycle"
```

### Task 4: Make Bilibili collection emit video-bundle jobs by default

**Files:**

- Modify: `spiders/bilibili.py`
- Modify: `collect.py`
- Modify: `config.py`
- Modify: `tests/test_spider_metadata.py`
- Modify: `tests/test_tools.py`

**Step 1: Write failing tests**

Assert a bounded Bilibili collection produces `artifact_kind=video_bundle`, uses BV + CID/part stable identity, stores no live DASH/subtitle signed query, preserves caption availability/provenance inputs and applies finite part/video bounds.

Also assert legacy rows already in the database remain `audio`; do not silently reinterpret history.

**Step 2: Run focused tests**

Expected: fail because the current spider emits audio URL records.

**Step 3: Implement default video job emission**

Reuse structured view/player metadata. Keep any Bilibili audio-only collection as an explicit compatibility mode, but choose its eventual CLI/config name only in this task and expose it in `--help` before documenting it.

**Step 4: Run tests**

Expected: pass and prove collection downloads no media bytes.

**Step 5: Commit**

```bash
git add spiders/bilibili.py collect.py config.py tests/test_spider_metadata.py tests/test_tools.py
git commit -m "feat: collect Bilibili video bundle jobs"
```

### Task 5: Add YouTube as a bounded collect adapter

**Files:**

- Create: `spiders/youtube.py`
- Modify: `collect.py`
- Modify: `config.py`
- Modify: `tests/test_spider_metadata.py`
- Modify: `tests/test_tools.py`

**Step 1: Write failing tests**

Cover canonical credential-free single-video URLs, playlist/live rejection, complete-parent-only jobs, native caption language policy, rights/AI/speaker-review defaults, finite per-run source count and no clip generation.

**Step 2: Run focused tests**

Expected: fail because `collect.py` has no YouTube adapter.

**Step 3: Implement metadata-only collection**

Use yt-dlp structured inspection without downloading. Store the sanitized artifact spec and stable video ID. Do not persist format dictionaries or signed media/subtitle URLs.

**Step 4: Run tests**

Expected: pass and assert no media files were created.

**Step 5: Commit**

```bash
git add spiders/youtube.py collect.py config.py tests/test_spider_metadata.py tests/test_tools.py
git commit -m "feat: collect complete YouTube video jobs"
```

### Task 6: Route claimed jobs through artifact handlers in main

**Files:**

- Create: `media_handlers.py`
- Modify: `main.py`
- Modify: `downloader.py`
- Modify: `storage.py`
- Create: `tests/test_media_handlers.py`
- Modify: `tests/test_downloader.py`

**Step 1: Write failing routing tests**

Use temporary databases and synthetic media. Assert:

- `audio` calls the existing downloader unchanged;
- `video_bundle` calls the correct platform adapter;
- a bundle is not marked done until its audit passes;
- local path is a file for audio and a directory for bundles;
- lease renewal covers long ffmpeg/video work;
- an unknown kind fails closed without deleting staged data.

**Step 2: Run focused tests**

Expected: fail at missing handler registry.

**Step 3: Implement a small explicit registry**

Use a fixed mapping rather than dynamic imports from database values:

```python
HANDLERS = {
    "audio": AudioArtifactHandler,
    "video_bundle": VideoBundleArtifactHandler,
}
```

Platform selection inside the video handler must also use an explicit source map.

**Step 4: Re-run focused tests**

Expected: pass.

**Step 5: Commit**

```bash
git add media_handlers.py main.py downloader.py storage.py \
  tests/test_media_handlers.py tests/test_downloader.py
git commit -m "feat: download queued media artifacts"
```

### Task 7: Define the shared sidecar envelope and per-kind validators

**Files:**

- Modify: `background.py`
- Modify: `media_bundle.py`
- Modify: `tests/test_background.py`
- Modify: `tests/test_media_bundle.py`
- Modify: `docs/SIDECAR-SCHEMA.md`

**Step 1: Write failing schema tests**

Require common identity, artifact kind, acquisition policy, content/caption language, caption provenance, AI generation, rights, file closure and toolchain. Assert manual/automatic/unknown are not inferred from each other or from media AI fields.

**Step 2: Run tests**

Expected: fail until both audio and bundle sidecars satisfy the common envelope.

**Step 3: Implement additive schema versions**

Do not rewrite historical sidecars in place. Validators must accept documented historical versions and produce an explicit migration/repair recommendation.

**Step 4: Re-run tests and docs checker**

```bash
python -m unittest tests.test_background tests.test_media_bundle -v
python scripts/check_docs.py
```

**Step 5: Commit**

```bash
git add background.py media_bundle.py tests/test_background.py \
  tests/test_media_bundle.py docs/SIDECAR-SCHEMA.md
git commit -m "feat: unify media sidecar envelopes"
```

### Task 8: Integrate stats, viewer, retry and doctor

**Files:**

- Modify: `main.py`
- Modify: `db_viewer.py`
- Modify: `doctor.py`
- Modify: `storage.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_storage.py`

**Step 1: Write failing operational tests**

Require counts by source, artifact kind and status; ensure failed bundle retry is bounded; ensure doctor detects schema drift, missing bundle roots and a low disk reserve without mutating the database.

**Step 2: Run tests**

Expected: fail at missing artifact-aware reporting.

**Step 3: Implement additive reporting**

Preserve existing audio stats output and add separate artifact sections. Do not invent silent migration in `db_viewer.py`.

**Step 4: Run tests**

Expected: pass.

**Step 5: Commit**

```bash
git add main.py db_viewer.py doctor.py storage.py tests/test_tools.py tests/test_storage.py
git commit -m "feat: report unified media queue state"
```

### Task 9: Import completed standalone bundles without redownload

**Files:**

- Create: `import_media_bundles.py`
- Create: `tests/test_import_media_bundles.py`
- Modify: `doctor.py`

**Step 1: Write failing dry-run tests**

Given synthetic valid/invalid Bilibili and YouTube bundles, report intended inserts, duplicates, path escapes, hash failures and conflicts without writing.

**Step 2: Implement read-only dry-run first**

Require explicit `--apply` for database mutation. Before apply, require a SQLite backup path and audit every candidate. Never copy, delete or redownload media.

**Step 3: Run tests**

```bash
python -m unittest tests.test_import_media_bundles -v
```

Expected: pass for dry-run/apply against temporary databases.

**Step 4: Commit**

```bash
git add import_media_bundles.py tests/test_import_media_bundles.py doctor.py
git commit -m "feat: import audited legacy media bundles"
```

### Task 10: Turn standalone CLIs into compatibility wrappers

**Files:**

- Modify: `bilibili_dataset.py`
- Modify: `youtube_dataset.py`
- Modify: `tests/test_bilibili_dataset.py`
- Modify: `tests/test_youtube_dataset.py`
- Modify: `docs/DOWNLOAD-GUIDE.md`

**Step 1: Write failing wrapper tests**

Assert the formal mode points users to collect/main. Preserve offline inspect/audit/repair behavior needed for recovery. Any direct download compatibility mode must print a deprecation warning and may not become the documented default.

**Step 2: Implement the smallest wrappers**

Do not remove proven parsers or validators. Move orchestration ownership, not platform knowledge.

**Step 3: Run focused and full tests**

```bash
python -m unittest tests.test_bilibili_dataset tests.test_youtube_dataset -v
bash scripts/test.sh
```

Expected: pass.

**Step 4: Commit**

```bash
git add bilibili_dataset.py youtube_dataset.py \
  tests/test_bilibili_dataset.py tests/test_youtube_dataset.py docs/DOWNLOAD-GUIDE.md
git commit -m "refactor: make dataset CLIs compatibility tools"
```

### Task 11: Synchronize user and agent documentation

**Files:**

- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/DOWNLOAD-GUIDE.md`
- Modify: `docs/SIDECAR-SCHEMA.md`
- Modify: `docs/architecture.md`
- Modify: `docs/design/architecture.md`
- Modify: `skills/audiospider-project/SKILL.md`
- Modify: `skills/bilibili-video-dataset/SKILL.md`
- Modify: `skills/youtube-caption-dataset/SKILL.md`

**Step 1: Compare every documented command with `--help`**

Delete or label any command that is not implemented. The formal path must be collect/database/main for every source.

**Step 2: Update examples after code exists**

Add the exact implemented bounded commands for Bilibili and YouTube. Do not revive standalone download as the primary tutorial.

**Step 3: Run documentation checks**

```bash
python scripts/check_docs.py
git diff --check
```

Expected: pass with no broken relative links.

**Step 4: Commit**

```bash
git add README.md docs skills
git commit -m "docs: make unified media flow the primary path"
```

### Task 12: Production migration rehearsal and cutover

**Files:**

- Create: `docs/reports/YYYY-MM-DD-unified-media-migration.md`

**Step 1: Snapshot production without mutation**

Record host, branch/commit, active processes, database integrity, per-source/status counts, existing bundle counts and disk usage.

**Step 2: Back up SQLite with its backup API**

Store the backup outside the checkout and record SHA-256. Do not copy a live WAL database with plain `cp`.

**Step 3: Run migration dry-run**

Expected: all legacy audio rows map to `audio`; valid standalone bundles map to `video_bundle`; every conflict is listed.

**Step 4: Apply schema and import in bounded batches**

Stop formal writers, apply once, run integrity check, import audited bundles, then re-enable collection/download.

**Step 5: Run full offline gate**

```bash
bash scripts/test.sh
python scripts/check_docs.py
python doctor.py
python main.py stats
```

Expected: no hard failures.

**Step 6: Run three bounded real probes**

Use one ordinary audio, one Bilibili part and one YouTube parent. Collection must add metadata jobs only; download them sequentially with one worker.

**Step 7: Verify artifacts**

Use ffprobe, JSON parsing, SHA-256 closure, caption language/provenance checks and database/path reconciliation. Require zero incomplete staging and no signed URL/Cookie persistence.

**Step 8: Write the evidence report**

Record exact counts, bytes, paths, commands, failures, rights limitations and whether any optional caption was unavailable.

**Step 9: Commit only after review**

```bash
git add docs/reports/YYYY-MM-DD-unified-media-migration.md
git commit -m "docs: record unified media migration validation"
```

Do not push unless explicitly authorized.
