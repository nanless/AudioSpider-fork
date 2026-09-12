# Unified collection runbook

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
```

Start bounded. The following are ordinary non-batch probes; never reuse their unscoped download commands
for `multispeaker-video-100-20260912`. Collection writes metadata jobs; download writes large files.

```bash
python probe.py --source bilibili --keywords "人物访谈 长视频" --search-pages 1 --videos 1 --parts 1
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 python probe.py --source youtube --videos 1
python collect.py --spiders bilibili
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 python collect.py --spiders youtube
python main.py --source bilibili --artifact-kind video_bundle --limit 1 --workers 1
python main.py --source youtube --artifact-kind video_bundle --limit 1 --workers 1
python scripts/audit_media_queue.py
```

For simultaneous Bilibili and YouTube long-video batches, first verify the deployed revision includes
the lease heartbeat and scoped expired-row recovery. Each active batch should have one `claimed_by`
identity and its `lease_expires_at` values should move forward after one heartbeat interval (60 seconds
with the default two-hour lease). Starting one source must not change another source's statuses.

For a planned restart, identify the exact PID and its `claimed_by` worker identity, stop that process and
verify it exited, then make a consistent SQLite backup. Reset only rows that are still
`status='downloading' AND claimed_by=<that-worker>` and also match the intended source/category/language/
artifact/time/batch IDs or job keys. Pending rows need no change; retry failed rows only through an
explicit scoped `--retry-failed`; never change another worker's claims. Restart only the source/batch that
must load the deployed code, and preserve completed bundles plus resumable staging files.
Use `scripts/recover_download_claims.py` for this operation: dry-run first, review every returned row, then
repeat the exact command with `--apply`. Do not replace it with a broad manual SQL update.

If disk is below the configured reserve, stop. Do not lower the reserve merely to force a run.
Use `--allow-bilibili-cookie` only after explicit user authorization; otherwise ambient cookies are ignored.

## Exact cross-platform multispeaker batch

For `multispeaker-video-100-20260912`, use the total index plus both formal manifests; never substitute
keyword search or an example file. The six required cells are Bilibili Chinese and YouTube English, each
with `影视=20`, `访谈=20`, and `会议论坛=10`. Validate the immutable candidate set first:

```bash
python -m unittest tests.test_multispeaker_batch_manifests
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
AUDIOSPIDER_BILIBILI_MANIFEST=config/bilibili_multispeaker_50_20260912.json \
python collect.py --spiders bilibili
AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_multispeaker_50_20260912.json \
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=50 python collect.py --spiders youtube
```

The entries deliberately retain `speaker_count=null/needs_review` and
`candidate_metadata.multi_speaker_evidence.status=candidate_unverified`; discovery context must not be
reported as acoustic verification. All items are complete platform videos, use same-language captions
best effort, and never invoke YouTube clip generation.

Keep one writer. Every normal or `--retry-failed` claim for this batch must include
`--batch-id multispeaker-video-100-20260912`; source/category/language are additional bounds. Finish with
`scripts/audit_media_queue.py` and the batch auditor's `--require-complete`; only its exact manifest IDs
and six-cell counts establish completion. Full operating and credential details are in
`skills/multispeaker-video-batch/references/runbook.md`.

For Bilibili caption-only repair, dry-run first, then use `--apply` after reviewing the exact bundle set.
The repair makes a SQLite backup, locks each job, verifies the old disk/DB fingerprint twice, displaces all
old caption payloads into a same-filesystem rollback directory, and updates sidecar plus DB by compare-and-swap.
`invalid_timeline` is deliberately excluded from default discovery so bad platform tracks cannot starve the
queue; retry it only when the platform track may have changed:

```bash
python scripts/backfill_bilibili_captions.py
python scripts/backfill_bilibili_captions.py --apply --allow-bilibili-cookie
python scripts/backfill_bilibili_captions.py --retry-invalid-timeline
python scripts/backfill_bilibili_captions.py --apply --allow-bilibili-cookie \
  --retry-invalid-timeline
```

Do not loosen the two-second media-tail tolerance to force acceptance. A quarantined `.rejected.json` is
evidence, not a usable transcript; it contains preserved cue content and safe platform structure with
transport credentials removed.

For a completed Bilibili bundle without a valid same-language platform track, use the same repair path:

```bash
python scripts/backfill_bilibili_captions.py --help
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr --apply
python scripts/audit_media_queue.py
```

Select the deployed Paddle backend and tuning only through the current `--ocr-*` flags shown by help.
OCR needs no Cookie and must preserve MP4/WAV hashes. Apply shares backup, exact lock, rollback and CAS.
`downloaded` remains unreviewed; report precision/recall, CER, timing, false-cue rate and RTF before
production acceptance. After an unclean kill, preserve backup/rollback evidence and audit first.

Declare a requested batch complete only by checking its baseline or manifest job keys and distinct
`source_id` values: every intended row must be `done` and every bundle must pass its validator plus
`scripts/audit_media_queue.py`. A process exit or a larger whole-database `done` total is not completion.
