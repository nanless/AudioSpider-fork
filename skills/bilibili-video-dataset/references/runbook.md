# Bilibili unified bundle runbook

The main route is `collect.py -> audiospider.db -> main.py`; do not operate a separate queue.

For an immutable target list, activate formal manifest mode and never fall back to search:

```bash
python -m unittest tests.test_multispeaker_batch_manifests
AUDIOSPIDER_BILIBILI_MANIFEST=config/bilibili_multispeaker_50_20260912.json \
python collect.py --spiders bilibili
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
```

The current 50-parent Bilibili candidate manifest is Chinese-family and exactly
`影视/screen_media=20`, `访谈/interview_roundtable=20`, and
`会议论坛/conference_forum=10`. Every selected part is downloaded whole. Empty `parts` means the
validated bounded part policy in the manifest, not permission to split, clip, or invent parts.
`candidate_metadata` may explain why an item was selected, but
`candidate_unverified` plus `speaker_count=null/needs_review` must stay unverified until actual listening.

1. Set a controlled search scope and explicit content language. The default configured batch is Chinese;
   use `AUDIOSPIDER_BILIBILI_CONTENT_LANGUAGE=en` for a reviewed English batch.
2. Run a one-video/one-part probe first. Confirm canonical BV page, part, CID, `job_key`, duration,
   rights state and native-language caption inventory.
3. Collect with `python collect.py --spiders bilibili`.
4. For an ordinary non-batch probe only, download with
   `python main.py --source bilibili --artifact-kind video_bundle --limit 1 --workers 1`. Exact batches
   must instead use the mandatory `--batch-id` commands below.
5. Verify MP4 has video+audio, WAV is PCM16/16 kHz/mono, every valid visible matching caption has
   sanitized JSON/VTT/TXT and manual/automatic/unknown provenance, every rejected timeline has only a
   `.rejected.json`, and no signed URL or credential is persisted.
6. Run `python scripts/audit_media_queue.py`.
7. For a completed bundle that needs caption-only repair, first run
   `python scripts/backfill_bilibili_captions.py --limit N` and review the
   dry-run list. Only an authorized apply run may add `--apply`; do not bypass
   its SQLite backup, exact job lock, atomic sidecar or rollback guards.

The worker must re-resolve the current part and require its CID to equal the collected CID. A mismatch is
a source revision failure, never permission to download the new content under the old job.

For caption-only repair, dry-run first and review the exact bundle set. Apply mode creates a SQLite
backup, locks each job, verifies disk/DB identity twice, displaces all old caption payloads into a
same-filesystem rollback directory, validates the replacement sidecar and commits by compare-and-swap.
It must never redownload or rewrite MP4/WAV:

```bash
python scripts/backfill_bilibili_captions.py --limit N
python scripts/backfill_bilibili_captions.py --limit N --apply --allow-bilibili-cookie
```

Rows marked `invalid_timeline` are excluded by default. Retry them only when the platform track may have
changed, using `--retry-invalid-timeline`; do not loosen the two-second media-tail tolerance merely to
force acceptance.

Caption payload names are deterministic and collision-safe:
`captions.<language>.<kind>.<track-id>.<1-based-index>.json/.vtt/.txt`; rejected tracks use the same
base plus `.rejected.json` and never have derived VTT/TXT. A repair is complete only when its result has
no failures, every affected bundle passes `validate_bundle`, and the unified queue audit passes.

When a server loopback proxy is required, use only `AUDIOSPIDER_BILIBILI_PROXY` for the Bilibili-scoped
process. Verify one allowed Bilibili request succeeds and an unrelated HTTPS host is rejected with 403.
Do not enable ambient proxy discovery or persist the proxy endpoint.

Default operation is anonymous. Only after explicit authorization may an operator privately set
`BILIBILI_COOKIE` and add `--allow-bilibili-cookie`; never print or persist it.

Keep one writer. Every normal or failed-row Bilibili claim for this batch must bind queue metadata with
`--batch-id multispeaker-video-100-20260912`; category and language remain additional filters:

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source bilibili --category '影视' --language zh \
  --artifact-kind video_bundle --limit 1 --workers 1 --format original
python main.py --batch-id multispeaker-video-100-20260912 \
  --source bilibili --category '影视' --language zh \
  --artifact-kind video_bundle --retry-failed --limit 1 --workers 1 --format original
```

Use the matching controlled category for the other two cells. Never omit `--batch-id` on a new-batch
download or retry.

For burned-in subtitles without a valid same-language platform track, keep the same bundle and command:

```bash
python scripts/backfill_bilibili_captions.py --help
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr --apply
python scripts/audit_media_queue.py
```

Choose the deployed Paddle backend/tuning only through current `--ocr-*` help. OCR needs no Cookie and
must preserve MP4/WAV hashes. Store `derived_text.visual_ocr` and `visual_ocr.json`; only a nonempty cue
set adds VTT/TXT. The result stays `human_review_status=unreviewed`. Review scene text, watermarks,
danmaku, lower thirds, no-subtitle and bilingual samples. Apply shares backup, exact lock, rollback,
validator and CAS; after SIGKILL/power loss retain recovery artifacts and audit before changes.

OCR receives no Cookie or proxy environment. If a logged-in platform refresh is explicitly authorized,
perform that separate API step first with the minimum Bilibili-origin allow-list kept in memory and sent
only to `api.bilibili.com`; never forward it to subtitle/media CDN, ffmpeg, Paddle, or a generic child.
