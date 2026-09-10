# Repository workflow

1. Connect to `dev_L4_1gpus`; inspect `git status`, current commit, writers, disk and `doctor.py`.
2. Develop in a `codex/*` worktree. Never edit the production checkout while a downloader writes.
3. Treat `collect.py -> audiospider.db -> main.py` as the only formal orchestration path.
4. Keep ordinary audio jobless for compatibility; every Bilibili/YouTube video job must carry
   `artifact_kind=video_bundle`, `source_id`, immutable `job_key`, and a signed-URL-free spec.
5. Run full unittest, pip check, docs link check, diff check, an old-DB migration on a backup copy,
   and bounded real probes before commit.
6. Before production schema/data mutation, stop writers and create a SQLite backup with its backup API.
7. Commit on the worktree branch, merge fast-forward into main only from a clean checkout, push, then
   verify local/remote commit identity.
8. For video data, run `scripts/audit_media_queue.py` and reconcile every DB row with the filesystem.
