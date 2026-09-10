# Repository workflow

1. Connect to `dev_L4_1gpus`; inspect `git status`, current commit, writers, disk and `doctor.py`.
2. Develop in a `codex/*` worktree. Never edit the production checkout while a downloader writes.
3. Treat `collect.py -> audiospider.db -> main.py` as the only formal orchestration path.
4. Keep ordinary audio jobless for compatibility; every Bilibili/YouTube video job must carry
   `artifact_kind=video_bundle`, `source_id`, immutable `job_key`, and a signed-URL-free spec.
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
8. For video data, run `scripts/audit_media_queue.py` and reconcile every DB row with the filesystem.
