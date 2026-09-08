# AudioSpider Documentation System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a beginner-friendly Chinese documentation hierarchy and add supported environment-health and controlled-live-probe tooling.

**Architecture:** Keep the existing CLI programs. Add `doctor.py` for read-only diagnostics, `probe.py` for bounded live-source checks, thin scripts for common workflows, and four progressive documentation layers linked from a rewritten root README.

**Tech Stack:** Python 3.11, argparse, asyncio, aiohttp, SQLite, ffmpeg/ffprobe, Bash, Markdown, unittest.

---

### Task 1: Make documentation trackable

**Files:** Modify `.gitignore`; create the `docs/` hierarchy.

1. Remove the blanket `docs/` ignore rule.
2. Add a documentation index and section indexes.
3. Verify every Markdown file is visible to Git.

### Task 2: Add supported operator tools

**Files:** Create `doctor.py`, `probe.py`, `scripts/bootstrap_conda.sh`, `scripts/test.sh`, `scripts/probe.sh`.

1. Add failing tests for diagnostic and probe argument behavior.
2. Implement read-only health checks with JSON and human output.
3. Implement bounded source probes using temporary databases.
4. Run CLI help and local-only tests.

### Task 3: Rewrite the beginner README

**Files:** Replace `README.md`; create `.env.example`.

1. Explain the three-stage workflow in plain Chinese.
2. Provide Conda setup, doctor, probe, collect, download and stats commands.
3. Add safety warnings, data layout, recovery and next-step links.

### Task 4: Write progressive documentation

**Files:** Create documents under `docs/getting-started`, `docs/guides`, `docs/reference`, and `docs/design`.

1. Write first-run and concepts tutorials.
2. Write discovery, download, operation and troubleshooting guides.
3. Document every CLI, environment variable, database table and adapter.
4. Document architecture, state transitions and security boundaries.

### Task 5: Verify and deliver

1. Run all unittests with `ResourceWarning` treated as an error.
2. Run `doctor.py`, CLI help, shell syntax and Python compilation.
3. Check all relative Markdown links and documented local paths.
4. Run a bounded real `podcast_rss` probe on the server.
5. Confirm `git diff --check` and report files changed.
