# Repository workflow

1. Connect to `dev_L4_1gpus`; inspect `git status`, current commit, writers, disk and `doctor.py`.
2. Develop in a `codex/*` worktree. Never edit the production checkout while a downloader writes.
3. Treat `manifest -> collect.py -> audiospider.db -> main.py` as the only formal orchestration path for
   exact batches. Bilibili must use its formal manifest mode; live search cannot reproduce a fixed quota.
4. Keep ordinary audio jobless for compatibility; every Bilibili/YouTube video job must carry
   `artifact_kind=video_bundle`, `source_id`, immutable `job_key`, and a signed-URL-free spec.
   Every `multispeaker-video-100-20260912` download or failed-row retry must pass
   `--batch-id multispeaker-video-100-20260912` to `main.py` so the atomic claim is bound to queue
   metadata; source/category filters alone are insufficient.
5. Run full unittest, pip check, docs link check, diff check, an old-DB migration on a backup copy,
   and bounded real probes before commit.
6. Stop all writers and create a SQLite backup before schema migration, whole-database copying or broad
   state mutation. A caption-only repair may instead coexist with unrelated workers only through its exact
   job lock, double disk/DB fingerprint check and short compare-and-swap transaction; never repair a job
   currently owned by a downloader.
7. Commit on the isolated worktree branch, then integrate the reviewed commit into a clean production
   checkout by fast-forward or explicit cherry-pick. Deployed Python processes do not hot-reload: when a
   running source/batch needs the fix, gracefully restart only that exact worker after preserving its
   resumable state. Push and verify local/remote commit identity.
8. For video data, run `scripts/audit_media_queue.py`. For an immutable batch, additionally run
   `scripts/audit_multispeaker_batch.py --batch-index <index> --db audiospider.db --require-complete`;
   reconcile the exact manifest identities and quota cells, not whole-database totals.
