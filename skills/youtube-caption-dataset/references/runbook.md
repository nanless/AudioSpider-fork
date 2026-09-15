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
Bind both ends to loopback. Allow `youtube.com` and its subdomains plus `googlevideo.com` and its
subdomains, all on port 443. Reject deceptive suffixes, non-443 targets and non-public DNS answers.
Keep the CONNECT proxy at `127.0.0.1:18797` and the PO-token provider at `127.0.0.1:4416` on both
hosts. A remapped port breaks the corresponding endpoint. Prove a YouTube request
succeeds and an unrelated host is rejected with 403 before any claim.

Scope every proxy variable to the exact YouTube command. Set upper- and lowercase HTTP(S) forms, clear
both `ALL_PROXY` forms, and keep both `NO_PROXY` forms loopback-only. Persist no proxy endpoint,
credential or signed URL, and stop the bounded transport after work finishes.

Classify failures before authentication: preserve deterministic `.staging/<job_key>` `.part` files,
give transfer/read/proxy failures a one-item anonymous canary, and treat `Sign in to confirm you're not
a bot` as separate evidence. Successful anonymous rows prove login is not a universal prerequisite.

Bilibili login authorization does not authorize Google/YouTube Cookie access. After separate explicit
authorization, the macOS-side bounded canary is:

```bash
python scripts/run_youtube_edge_gate.py \
  --batch-id multispeaker-video-100-20260912 \
  --category '影视' --limit 1
```

The helper resolves Edge Local State's current `last_used` profile (or an explicitly bounded
`Default`/`Profile N` directory), reads that Cookie SQLite read-only and selects only allow-listed `.youtube.com` session
entries. It decrypts and validates them in the local process, then writes a bounded JSON array directly
to remote `main.py --allow-youtube-cookie` stdin. It never writes a cookie file or puts a value in argv,
stdout, stderr or environment. If the required login entries are missing/expired, refresh the current
Edge YouTube login; never broaden to `.google.com`, print values or hand-export a cookie file.

The server gate must reject every scope except bounded `download`, explicit `--source youtube`, effective
`video_bundle`, explicit batch/category/language, `--workers 1`, no grouped claims and no `--loop`.
It consumes stdin before claiming a row, removes ambient
`YOUTUBE_COOKIE_JSON`, and installs the entries into an in-memory `.youtube.com` CookieJar for both
inspection and transfer. Googlevideo receives no login Cookie. Failure JSON, worker IPC and logs must
redact values. Never substitute `--cookies-from-browser`, a copied profile, Netscape cookie file, global
`Cookie` header or argv secret.

Authenticated extraction deliberately selects the yt-dlp `mweb` client. Keep Deno 2.9,
`yt-dlp-ejs==0.8.0`, `bgutil-ytdlp-pot-provider==2.0.0`, and the pinned yt-dlp version from the environment
files. Confirm Deno/EJS and PO-token-provider discovery before widening transport or blaming credentials;
do not fall back to the known-broken logged-in `tv_downgraded` path.

`main.py` currently has no exact YouTube `job_key` claim option. A category-scoped `--limit 1` claims
the first eligible row in database order, so verify the claimed identity immediately. Do not reorder
rows or edit queue statuses to force a canary. Expand only after the authenticated single-item canary
succeeds; keep one writer, finite limits and the exact batch/cell filters throughout the retry.
After every authorized run, the fail-closed leak audit must stream large candidate files, inspect common
reversible encodings and every SQLite table, and return `error` rather than `clean` for unreadable candidates.

```bash
python youtube_dataset.py --output downloads/youtube audit
python scripts/audit_media_queue.py
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db --require-complete
```

Report the exact manifest video IDs/job keys, status counts, full-parent duration/bytes, MP4/WAV probe,
caption kind/language, hashes, rights/AI/speaker review state, failures and staging. Whole-database totals
or a clean process exit are not batch completion. The batch auditor is a manifest/queue count gate; also
run every target bundle validator and confirm there is no active writer or target-batch staging/partial
artifact before declaring completion. Source-wide audit failures caused solely by unrelated historical
staging must be reported separately, not "fixed" by deleting evidence. If the repository still lacks a
batch-scoped bundle validator, implement it before the final completion claim.
