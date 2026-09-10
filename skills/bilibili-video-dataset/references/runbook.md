# Bilibili unified bundle runbook

The main route is `collect.py -> audiospider.db -> main.py`; do not operate a separate queue.

1. Set a controlled search scope and explicit content language. The default configured batch is Chinese;
   use `AUDIOSPIDER_BILIBILI_CONTENT_LANGUAGE=en` for a reviewed English batch.
2. Run a one-video/one-part probe first. Confirm canonical BV page, part, CID, `job_key`, duration,
   rights state and native-language caption inventory.
3. Collect with `python collect.py --spiders bilibili`.
4. Download with `python main.py --source bilibili --artifact-kind video_bundle --limit 1 --workers 1`.
5. Verify MP4 has video+audio, WAV is PCM16/16 kHz/mono, every visible matching caption has
   JSON/VTT/TXT and manual/automatic/unknown provenance, and no signed URL is persisted.
6. Run `python scripts/audit_media_queue.py`.
7. For a completed bundle that needs caption-only repair, first run
   `python scripts/backfill_bilibili_captions.py --limit N` and review the
   dry-run list. Only an authorized apply run may add `--apply`; do not bypass
   its SQLite backup, exact job lock, atomic sidecar or rollback guards.

The worker must re-resolve the current part and require its CID to equal the collected CID. A mismatch is
a source revision failure, never permission to download the new content under the old job.

Default operation is anonymous. Only after explicit authorization may an operator privately set
`BILIBILI_COOKIE` and add `--allow-bilibili-cookie`; never print or persist it.
