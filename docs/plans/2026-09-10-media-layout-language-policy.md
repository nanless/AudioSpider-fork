# Media Layout and Caption Language Policy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Put all Bilibili video bundles under `downloads/`, keep YouTube as parent videos only, and enforce content-matching platform captions.

**Architecture:** Reuse each dataset pipeline and add a shared policy function inside each platform module. Validate policy at manifest ingestion, post-download caption confirmation, and bundle audit; preserve historical clips and mismatched parents in recoverable archive directories.

**Tech Stack:** Python 3.11, unittest, ffmpeg/ffprobe, JSON manifests, filesystem rename, Git.

---

### Task 1: Write failing language-policy and clip-authorization tests

- Modify `tests/test_youtube_dataset.py`.
- Modify `tests/test_bilibili_dataset.py`.
- Assert English/Chinese/Yue/native mappings, manifest rejection, bundle audit rejection and explicit clip authorization.

### Task 2: Implement policy gates

- Modify `youtube_dataset.py` and `bilibili_dataset.py`.
- Validate requested caption languages against content language.
- Confirm selected/downloaded track language again before promotion.
- Require `--allow-clips` for the YouTube `clip` command.

### Task 3: Correct manifests and documentation

- Update initial/example manifests to remove cross-language fallbacks.
- Update README, guides, references, architecture, reports and changelog.
- Update `audiospider-project`, `youtube-caption-dataset` and `bilibili-video-dataset` Skills.

### Task 4: Migrate and audit real data

- Move Bilibili data to `downloads/bilibili-video-20260910`.
- Archive all YouTube clips and mismatched parent bundles without deletion.
- Inspect mismatched sources for native Chinese captions; re-download only if accepted.
- Run focused/full tests, docs check, strict audits, commit and push.
