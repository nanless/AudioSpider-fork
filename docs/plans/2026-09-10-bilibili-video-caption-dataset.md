# Bilibili Video and Caption Dataset Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an auditable Bilibili video dataset pipeline that downloads public multipart videos, platform captions and rich sidecars while preserving the legacy audio-only workflow.

**Architecture:** Keep `collect.py/main.py` unchanged. Put pure subtitle parsing and provenance rules in `bilibili_subtitles.py`; put manifest validation, public API inspection, bounded DASH download/merge, bundle promotion, repair and audit in `bilibili_dataset.py`. Store one self-contained bundle per BV part.

**Tech Stack:** Python 3.10+, aiohttp, ffmpeg/ffprobe, unittest, JSON manifests, SHA-256 sidecars.

---

## Task 1: Lock the subtitle provenance contract

**Files:**
- Create: `bilibili_subtitles.py`
- Create: `tests/test_bilibili_subtitles.py`
- Modify: `spiders/bilibili.py`
- Modify: `tests/test_spider_metadata.py`

1. Write failing tests for structured automatic, authored manual, ambiguous, conflicting and missing-field tracks.
2. Write failing JSON-to-cue/VTT/TXT tests for Unicode, timing validation and size/cue limits.
3. Implement the smallest parser and classifier that passes them.
4. Make the legacy spider emit the same provenance fields.

## Task 2: Validate manifests and inspect public parts

**Files:**
- Create: `bilibili_dataset.py`
- Create: `tests/test_bilibili_dataset.py`
- Create: `config/bilibili_sources.example.json`

1. Test accepted BV URLs/IDs, part selectors and bounded numeric fields.
2. Test rejection of credentials, non-HTTPS hosts, duplicate jobs, invalid rights/AI fields and unbounded manifests.
3. Mock view/player/playurl responses and test deterministic stream/caption selection.
4. Implement `inspect` with sanitized manifests and no signed URL persistence.

## Task 3: Download, merge and promote a complete bundle

**Files:**
- Modify: `bilibili_dataset.py`
- Modify: `tests/test_bilibili_dataset.py`

1. Test deterministic staging, lock behavior and resumable `.part` downloads with mocked public responses.
2. Implement bounded streaming downloads with per-redirect validation.
3. Merge DASH streams, extract WAV, convert subtitles and compute file records.
4. Validate the full bundle before atomic promotion; never trust metadata existence alone.

## Task 4: Add strict audit and repair

**Files:**
- Modify: `bilibili_dataset.py`
- Modify: `tests/test_bilibili_dataset.py`

1. Test empty dataset, missing members, hash mismatch, path escape, invalid provenance, bad media and incomplete staging.
2. Implement `audit` with non-zero exit for every closure failure.
3. Implement derived subtitle/text/sidecar repair without silently replacing source media.

## Task 5: Document every layer

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/design/architecture.md`
- Modify: `docs/reference/cli.md`
- Modify: `docs/reference/spiders.md`
- Create: `docs/guides/bilibili-video-datasets.md`
- Create: `docs/reference/bilibili-video-sidecars.md`

Explain the audio/video pipeline distinction, beginner commands, file layout, subtitle provenance, missing subtitles, AI/rights boundaries, resume, audit and troubleshooting in Chinese.

## Task 6: Remote real-data verification and delivery

1. Create an isolated `codex/bilibili-video` worktree from current `main` on `dev_L4_1gpus`.
2. Run unit tests and the full repository test suite in the `audiospider` Conda environment.
3. Inspect at least one manual-caption, one automatic-caption and one missing-caption public part when available.
4. Download a bounded initial batch, run ffprobe/hash/sidecar/audit checks, and record exact counts and bytes.
5. Commit in Chinese, fast-forward or merge safely to `main`, push `origin/main`, and update the repository-specific Codex skill.
