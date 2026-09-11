# YouTube Interview and Screen Dataset Implementation Plan

> **Historical and superseded.** Do not execute the standalone/automatic-clip
> workflow below. Current jobs use the unified SQLite pipeline and retain full
> parent videos; see [the unified download guide](../DOWNLOAD-GUIDE.md).

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a reproducible, auditable YouTube pipeline for long interview videos and caption-aligned film/TV-style short clips, then download and validate a real sample batch.

**Architecture:** Keep the existing SQLite audio queue unchanged. Add a standalone `youtube_dataset.py` CLI backed by small pure helper modules, JSON source manifests, deterministic output paths, yt-dlp metadata/download APIs, ffmpeg derivatives, JSONL run manifests, and a read-only audit command.

**Tech Stack:** Python 3.11, yt-dlp 2026.08.19, ffmpeg/ffprobe, JSON/JSONL, WebVTT, unittest.

---

## Task 1: Define profiles and input schema

**Files:**

- Create: `youtube_dataset.py`
- Create: `tests/test_youtube_dataset.py`
- Create: `config/youtube_sources.example.json`

1. Write failing tests for profile duration bounds, speaker metadata states, YouTube URL validation, stable video IDs and safe output paths.
2. Implement profile dataclasses/constants and manifest validation.
3. Run `python -m unittest tests.test_youtube_dataset -v`.
4. Commit the schema and validation slice.

## Task 2: Inspect metadata and select captions

**Files:**

- Modify: `youtube_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

1. Write failing tests proving manual captions beat automatic captions and automatic captions are labeled `platform_auto`.
2. Add a thin yt-dlp adapter that uses structured dictionaries and sanitized info instead of parsing human-readable stdout.
3. Add `inspect` output as append-only JSONL with per-item success/failure.
4. Verify no signed media or subtitle query string is persisted.

## Task 3: Download parent video, audio, captions and sidecar

**Files:**

- Modify: `youtube_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

1. Write tests for deterministic paths, download options, atomic sidecar writes and SHA-256 records.
2. Download public media at up to 720p and extract 16 kHz mono WAV.
3. Save the chosen platform VTT and normalized plain text.
4. Save a complete `metadata.json` with explicit caption and speaker provenance.

## Task 4: Generate subtitle-aligned short clips

**Files:**

- Create: `youtube_vtt.py`
- Modify: `youtube_dataset.py`
- Create: `tests/test_youtube_vtt.py`
- Modify: `tests/test_youtube_dataset.py`

1. Write failing tests for WebVTT timestamps, cue cleanup, duplicate removal and cue grouping.
2. Implement grouping constrained to 0.418–29.888 seconds with an 11.5-second target.
3. Generate MP4, WAV, relative VTT, transcript TXT and JSON sidecar per clip.
4. Add an offline synthetic-media integration test.

## Task 5: Audit outputs

**Files:**

- Modify: `youtube_dataset.py`
- Modify: `tests/test_youtube_dataset.py`

1. Write tests for missing bundle members, hash mismatch, invalid captions and duration violations.
2. Implement `audit` with machine-readable summary and nonzero exit on hard failures.
3. Report counts by profile, language, caption kind and speaker verification status.

## Task 6: Dependencies and documentation

**Files:**

- Modify: `requirements.txt`
- Modify: `requirements-lock.txt`
- Modify: `README.md`
- Modify: `docs/README.md`
- Create: `docs/guides/youtube-datasets.md`
- Create: `docs/reference/youtube-sidecars.md`
- Modify: `docs/reference/cli.md`
- Modify: `docs/design/data-flow.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`

1. Pin yt-dlp to the verified stable release.
2. Document beginner setup, source-manifest authoring, manual versus automatic captions, downloads, clipping, resuming and audits.
3. Document rights, privacy and platform-access boundaries.
4. Run the documentation checker.

## Task 7: Isolated environment and full verification

1. Sync the branch worktree to `dev_L4_1gpus`.
2. Update the existing `audiospider` Conda environment with `scripts/bootstrap_conda.sh`.
3. Run focused tests, `bash scripts/test.sh`, and a synthetic ffmpeg integration test.
4. Inspect the final diff and commit code/docs on `codex/youtube-media`.

## Task 8: Real discovery, download and evidence report

**Files:**

- Create: `config/youtube_sources.initial.json` only if sources are redistributable as URLs and the file contains no credentials.
- Create: `docs/reports/2026-09-09-youtube-dataset-validation.md`

1. Inspect candidate official/public videos without downloading.
2. Select captioned long interviews inside the duration target and captioned multilingual screen sources.
3. Download a bounded initial batch into the production `downloads/youtube` tree.
4. Generate screen clips, run the audit and record exact counts, durations, languages, caption kinds, bytes and failures.
5. Merge the tested branch into production only after all repository and data audits pass.
