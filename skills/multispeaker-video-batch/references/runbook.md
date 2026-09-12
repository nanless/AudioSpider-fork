# Exact multispeaker video batch runbook

## Contract and files

The current immutable batch is `multispeaker-video-100-20260912`:

| Source/language | `影视` / `screen_media` | `访谈` / `interview_roundtable` | `会议论坛` / `conference_forum` | Total |
|---|---:|---:|---:|---:|
| Bilibili / Chinese family | 20 | 20 | 10 | 50 |
| YouTube / English family | 20 | 20 | 10 | 50 |

Use exactly these repository files:

```text
config/multispeaker_video_100_20260912.batch.json
config/bilibili_multispeaker_50_20260912.json
config/youtube_multispeaker_50_20260912.json
```

Do not replace them with examples or live search. `source_revision` and `batch_id` stay equal to the
batch ID. `selection_slot` is unique inside the batch. Each item stays `require_caption=false` and uses
the controlled category/kind pair above.

These are candidate manifests, not verified speaker labels. Keep `speaker_count=null`,
`speaker_count_status=needs_review`, and candidate evidence `status=candidate_unverified` until a human
reviews the actual complete media. A title or participant list may justify selection for review, never
`verified_manual` or an exact count.

## Preflight and static validation

```bash
ssh dev_L4_1gpus
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
git status --short
git rev-parse --short HEAD
python doctor.py
python main.py stats
ps aux | grep -E 'collect.py|main.py|backfill_bilibili_captions.py'
df -h . downloads
python -m unittest tests.test_multispeaker_batch_manifests
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
```

The unit test is the offline six-cell/identity/provenance gate. The audit command is read-only against
the existing database and reports exact queued/done/missing parents. Save its JSON as the baseline
outside tracked source files if an operator needs a before/after record.

Allow only one formal collector/downloader writer. Do not deploy identity/schema code while it runs,
copy a live SQLite/WAL pair, globally reset leases, or start two sources concurrently during collection.

## Formal collection

Run each source sequentially through the shared collector:

```bash
AUDIOSPIDER_BILIBILI_MANIFEST=config/bilibili_multispeaker_50_20260912.json \
python collect.py --spiders bilibili

AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_multispeaker_50_20260912.json \
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=50 \
python collect.py --spiders youtube

python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
```

The Bilibili environment variable activates formal manifest mode; it must not fall back to keyword
search. The YouTube profiles are `youtube_screen_parents`, `youtube_interviews`, and
`youtube_conference_forums`. All three create complete-parent `video_bundle` jobs. The legacy
`youtube_screen_clips` profile is not part of this batch.

Rejected or unavailable items remain explicit gaps. Replace a candidate in the appropriate manifest
slot, rerun static validation, and recollect; never over-download another cell to hide the gap.

## Exact-download gate

`main.py --batch-id` reads the batch identity from each platform's validated queue metadata and applies
it inside the atomic pending/failed claim. It is mandatory for every download and retry in this batch.
Keep source, category, language and artifact kind as additional defense-in-depth filters; never omit the
batch filter merely because the category appears unique.

After that gate, start with one worker and one cell, then expand gradually:

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source bilibili --category '影视' --language zh \
  --artifact-kind video_bundle --limit 1 --workers 1 --format original
python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --category '影视' --language en \
  --artifact-kind video_bundle --limit 1 --workers 1 --format original
```

Repeat for `访谈` and `会议论坛`, auditing after each bounded run. A completed artifact must be
under `downloads/<source>/<dataset_category>/<source_id>/<job_key>/` and contain the complete MP4,
PCM16/16 kHz/mono WAV, metadata sidecar, and only the caption/OCR files declared by its closure.

Retry only the same exact batch and cell:

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --category '会议论坛' --language en \
  --artifact-kind video_bundle --retry-failed --limit 1 --workers 1 --format original
```

Use the analogous Bilibili source/language when retrying Bilibili. Never run this batch's normal or
failed-row claim without its exact `--batch-id`.

## Captions and Bilibili OCR

- Bilibili Chinese candidates accept only the Chinese/Mandarin/Cantonese family; YouTube English
  candidates accept only English-family tracks.
- Prefer and label platform manual tracks; preserve automatic/unknown provenance. `require_caption=false`
  retains genuine `missing` or `no_matching_language` cases without fake VTT/TXT. Network, parser, or
  authorization failures are not absence.
- For completed Bilibili bundles with no valid same-language platform track, use the existing caption
  backfill with explicit `--visual-ocr`, first dry-run, then one exact `--job-key` apply. OCR writes
  `derived_text.visual_ocr`, never rewrites platform caption provenance or MP4/WAV.
- Do not apply visual OCR to YouTube in this batch unless a separate design is explicitly requested.

## Cookie and proxy boundary

Cookie access and local proxying require explicit user authorization at the time of use. For Bilibili,
extract only the minimum Bilibili-origin Edge allow-list, keep it in memory, pass it through stdin or a
one-shot loopback bridge, add `--allow-bilibili-cookie`, and send it only to `api.bilibili.com`; never
send it to CDN, ffmpeg, OCR, logs, argv, disk, SQLite, or sidecars.

When a reverse proxy is authorized, Bilibili uses only the source-scoped
`AUDIOSPIDER_BILIBILI_PROXY`; YouTube uses a temporary allow-list proxy for YouTube/Googlevideo and only
the YouTube process receives its proxy environment. Verify an allowed host succeeds and an unrelated
host is rejected before collection. Keep tunnels bounded to the active run and persist no endpoint or
signed media URL.

## Completion and recovery

```bash
python scripts/audit_media_queue.py
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db --require-complete
```

Completion requires all six cells at exactly 20/20/10 per source, 100 distinct expected parents queued
and done, no missing/extras in the batch audit, and every bundle passing the unified validator/audit.
Report caption availability/provenance, OCR separately, bytes/duration, rights/AI/speaker review state,
failures and staging. Candidate evidence remains unverified until the separate human media review.

For interruption, identify the one writer and its exact claims, stop it gracefully, take a SQLite backup,
and use the repository's scoped recovery tooling in dry-run before apply. Never perform a global
`downloading -> pending` update or touch another writer's lease.
