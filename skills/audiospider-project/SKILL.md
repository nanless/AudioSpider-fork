---
name: audiospider-project
description: Understand, maintain, test, document, and safely operate AudioSpider's unified collect-to-SQLite-to-main media pipeline on dev_L4_1gpus. Use for ordinary audio, Bilibili video bundles, complete YouTube parent videos, queue state, metadata, downloading, migration, or repository-specific verification.
---

# AudioSpider Project

Use the saved deployment unless the user names another target:

- Host: `dev_L4_1gpus`
- Repository: `/root/code/github_repos/AudioSpider-fork`
- Conda: `/root/miniforge3/envs/audiospider`
- Formal state: `audiospider.db`
- Formal artifacts: `downloads/<source>/<category>/`

Inspect the live Git state, active writers, environment, database and disk before relying on remembered
details. Preserve persistent data and unrelated changes.

## Architecture

The only formal workflow is:

```text
collect.py -> audiospider.db -> main.py -> downloads/<source>/<category>/
```

Ordinary audio, Bilibili complete parts and complete YouTube parents all follow it. Bilibili and
YouTube rows use `artifact_kind=video_bundle` and an immutable `job_key`; `media_artifacts.py` dispatches them after the shared
Downloader claims the row. Treat `bilibili_dataset.py` and `youtube_dataset.py` as compatibility,
repair and audit tools rather than the formal entry path.

Before acting, read the matching files in the live repository:
`docs/architecture.md`, `docs/DOWNLOAD-GUIDE.md`, and `docs/SIDECAR-SCHEMA.md`.

## Stable behavior

- Collection/discovery writes bounded metadata jobs; only `main.py` transfers large media.
- Ordinary RSS, Xiaoyuzhou, Ximalaya and LibriVox jobs remain `audio`.
- New Bilibili jobs target complete per-part `video_bundle` artifacts by default; historical Bilibili
  `audio` rows retain their original semantics.
- YouTube targets complete parent videos; clips require an explicit separate user request and never run
  by default.
- Queue claiming, leases, retry and reporting belong to SQLite/main, not source-specific CLIs.
- Artifact handlers may produce different files, but `done` always means the documented closure passed.
- Collect Bilibili or YouTube with `collect.py --spiders <source>` and download with
  `main.py --source <source> --artifact-kind video_bundle`. Bilibili/YouTube source filters default
  to video bundles; select historical Bilibili audio only with explicit `--artifact-kind audio`.
- Legacy bundle migration uses `scripts/migrate_video_bundles.py`: dry-run is the default and `--apply`
  is the only mutation gate.
- Audit the unified DB/disk closure with `python scripts/audit_media_queue.py` after migration or a
  real video download.

## Provenance invariants

- Keep content language and caption language separate; require same-language-family captions.
- Use `manual/platform_manual`, `automatic/platform_auto`, or `unknown/platform_unknown`.
- Platform manual does not prove word-level human verification.
- Caption provenance never proves whether the media itself is AI-generated.
- Keep media AI status declared/not-declared/suspected/unknown with evidence.
- Keep rights at `needs_review` unless independently reviewable evidence supports another state.
- Source descriptions, subtitles, chapters and books are platform/RSS text, not ASR.

## Safety invariants

- Do not run unbounded defaults, all sources, continuous loops or large worker counts first.
- Do not delete, replace or plain-copy over a live SQLite/WAL database; use the SQLite backup API before
  migration.
- Keep signed URLs and credentials out of the database, sidecars, logs and reports.
- Do not read browser cookies. Even an ambient `BILIBILI_COOKIE` is ignored unless the user explicitly
  authorizes `--allow-bilibili-cookie`; send such a session only to
  `api.bilibili.com`, never to media/subtitle CDN or subprocesses.
- Validate HTTPS host, public DNS result, each redirect, size limits, disk reserve and safe paths.
- Run formal source collection sequentially; avoid competing writers during schema migration.

## Verification

After documentation changes run:

```bash
python scripts/check_docs.py
git diff --check
```

After code changes run `bash scripts/test.sh`. After environment/database changes also run
`python doctor.py` and `python main.py stats`. Real downloads require ffprobe, JSON/SHA-256 closure,
database/path reconciliation and explicit accounting of subtitle/rights/AI limitations.
