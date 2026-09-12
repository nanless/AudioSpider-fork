# YouTube complete-parent runbook

## Exact manifest batch

For `multispeaker-video-100-20260912`, the YouTube manifest contains 50 unique English candidates:

| Category | Content kind | Profile | Count |
|---|---|---|---:|
| `影视` | `screen_media` | `youtube_screen_parents` | 20 |
| `访谈` | `interview_roundtable` | `youtube_interviews` | 20 |
| `会议论坛` | `conference_forum` | `youtube_conference_forums` | 10 |

All entries use `require_caption=false`, retain the complete platform parent, and keep
`speaker_count=null/needs_review`. `candidate_metadata.*.status=candidate_unverified` is discovery
context, not verified language or speaker evidence.

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python -m unittest tests.test_multispeaker_batch_manifests tests.test_youtube_dataset tests.test_youtube_spider
AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_multispeaker_50_20260912.json \
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=50 \
python collect.py --spiders youtube
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
```

Collection may reject unavailable, live/upcoming, out-of-profile, or failed-inspection candidates.
Record the exact gap and replace the same manifest slot; never compensate with another category.

## Download and captions

Keep one downloader writer. Bind every pending or failed-row claim to the validated YouTube job metadata
with `--batch-id multispeaker-video-100-20260912`; source/category/language remain additional filters.

Start with one complete parent:

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --category '影视' --language en \
  --artifact-kind video_bundle --limit 1 --workers 1 --format original
```

Retry the same exact batch, never a category-only slice:

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --category '影视' --language en \
  --artifact-kind video_bundle --retry-failed --limit 1 --workers 1 --format original
```

Repeat bounded runs for `访谈` and `会议论坛`. Each bundle belongs under
`downloads/youtube/<dataset_category>/<video_id>/<job_key>/` and always contains complete
`source.mp4`, complete PCM16/16 kHz/mono `audio.wav`, and `metadata.json`. Do not call clip generation,
trim silence, or replace a parent with a derived segment.

English-family manual captions are preferred, automatic captions remain explicitly `platform_auto`,
and `missing`/`no_matching_language` best-effort results retain MP4/WAV without fake VTT/TXT. Extractor,
network, proxy, and parser failures remain failures.

## Proxy and completion

Only after explicit user authorization may the YouTube process use a temporary local reverse proxy.
Allow-list only YouTube/Googlevideo and required static/API suffixes; prove an allowed request succeeds
and an unrelated host is rejected. Scope proxy environment to the YouTube collector/downloader, unset
`ALL_PROXY`, persist no endpoint or credentials, and stop the tunnel after the bounded run.

```bash
python scripts/audit_media_queue.py
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db --require-complete
```

Report the exact manifest video IDs/job keys, status counts, full-parent duration/bytes, MP4/WAV probe,
caption kind/language, hashes, rights/AI/speaker review state, failures and staging. Whole-database totals
or a clean process exit are not batch completion.
