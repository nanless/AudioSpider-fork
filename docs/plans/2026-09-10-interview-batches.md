# Interview Batch Best-Effort Caption Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Collect and download 100 complete Chinese Bilibili interviews and 50 complete English YouTube interviews through AudioSpider's unified queue, retaining videos when same-language captions are unavailable.

**Architecture:** Keep `collect.py`, `audiospider.db`, and `main.py` as the only orchestration chain. Add an explicit YouTube `require_caption` policy and bounded Bilibili environment controls, then register two auditable batches whose video handlers either add native platform captions or publish a truthful caption-missing bundle.

**Tech Stack:** Python 3.11, SQLite/WAL, aiohttp, yt-dlp, ffmpeg/ffprobe, JSON manifests and sidecars, unittest.

---

### Task 1: Lock YouTube best-effort manifest semantics

**Files:**

- Modify: `youtube_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

**Steps:**

1. Add failing tests that normalize `require_caption=false` and preserve a missing-caption inspection.
2. Add strict boolean validation and include the field in deterministic `job_key` identity.
3. Keep legacy manifests strict by a documented default, while this batch opts into best effort explicitly.
4. Run the focused manifest and caption tests.

### Task 2: Produce valid parent bundles without captions

**Files:**

- Modify: `youtube_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

**Steps:**

1. Write a synthetic no-caption parent bundle test.
2. Make download options conditional: omit subtitle flags when no track is selected.
3. Build `metadata.json` with `caption.status=missing`, zero caption files, requested language and selection evidence.
4. Update bundle validation so `require_caption=false` accepts the MP4/WAV/metadata closure and strict mode rejects it.
5. Verify existing captioned bundle and clip tests remain green.

### Task 3: Pass policy through the unified YouTube spider

**Files:**

- Modify: `spiders/youtube.py`
- Modify: `tests/test_youtube_spider.py`

**Steps:**

1. Add tests that a valid no-caption inspection is queued only when `require_caption=false`.
2. Preserve inspection facts without signed URLs; set `transcript_status=not_provided` and neutral `text_source`.
3. Confirm the immutable `job_key` and unified video bundle record are unchanged otherwise.

### Task 4: Add bounded Bilibili batch controls

**Files:**

- Modify: `config.py`
- Modify: `.env.example`
- Modify: `tests/test_main.py` or a new configuration test

**Steps:**

1. Add positive environment bounds for search pages, videos per keyword and parts per video.
2. Add a controlled comma-separated keyword override without evaluating shell content.
3. Verify `content_language=zh`, 100 videos, one part, and interview-only keyword configuration.

### Task 5: Add batch manifests and operator documentation

**Files:**

- Create: `config/youtube_interviews_50_20260910.json`
- Modify: `docs/DOWNLOAD-GUIDE.md`
- Modify: `docs/SIDECAR-SCHEMA.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: relevant repository Skills

**Steps:**

1. Validate every YouTube URL/ID and record rights as `needs_review`.
2. Document exact collection/download commands, ephemeral Bilibili Cookie injection, resume and audit.
3. Document missing-caption sidecar semantics and do not call local ASR platform text.
4. Run Markdown link and Skill validation.

### Task 6: Verify, commit and deploy

**Files:** all changed files.

**Steps:**

1. Run the complete unittest suite, compileall, pip check, docs check and diff check.
2. Run one Bilibili authenticated caption probe and one YouTube captionless synthetic end-to-end test.
3. Commit with a detailed Chinese message, fast-forward merge to main and push.
4. Confirm `HEAD == origin/main` and production worktree is clean.

### Task 7: Run the two production batches

**Files:** operational data under `audiospider.db`, `downloads/`, and ignored `logs/`.

**Steps:**

1. Record pre-batch DB counts and create a SQLite backup.
2. Collect Bilibili with a 100-video, one-part Chinese interview scope.
3. Collect the 50-item English YouTube manifest with `require_caption=false`.
4. Download each source separately with bounded workers and resumable staging.
5. Re-run failed tasks only after classifying causes.
6. Run unified audit and produce exact per-batch coverage statistics.
