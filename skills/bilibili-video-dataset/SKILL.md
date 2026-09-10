---
name: bilibili-video-dataset
description: Collect, download, resume, audit, and document complete Bilibili per-part video bundles through AudioSpider's unified SQLite media pipeline on dev_L4_1gpus. Use for Bilibili video, DASH, CID, platform subtitles, or bundle provenance; do not route new formal work through the legacy audio-only or standalone dataset path.
---

# Bilibili Video Bundle

Work in `/root/code/github_repos/AudioSpider-fork` on `dev_L4_1gpus`, using the `audiospider`
Conda environment.

## Route

The formal route is:

```text
collect.py -> audiospider.db -> main.py -> downloads/bilibili/<category>/<source_id>/<job_key>/
```

Bilibili collection now defaults to `video_bundle`. Use `collect.py --spiders bilibili` to add bounded
configured jobs and `main.py --source bilibili --artifact-kind video_bundle` to download them. Historical Bilibili `audio` rows
remain compatible. The standalone `bilibili_dataset.py` is for inspection, repair, import validation
or an explicitly requested compatibility workflow; it is not the documented main path.

Before acting, read `docs/DOWNLOAD-GUIDE.md`, `docs/architecture.md`, and
`docs/SIDECAR-SCHEMA.md` in the live repository.

## Identity and bounds

- Accept a canonical credential-free BV URL/ID and an explicit part; map `?p=N` to the part instead of
  discarding it.
- Use CID as the concrete part identity; page number is a mutable human-facing position.
- Bind CID, language/caption policy, height and source revision into `job_key`; reject download if the
  current part CID no longer matches the collected CID.
- Bound source count, part count, duration, height, stream bytes, caption bytes and total bundle bytes.
- Never use an example manifest as the user's target.

## Artifact closure

Download video and audio DASH representations separately, bind resume to a signed-URL-free descriptor,
and merge explicit `v:0` plus `a:0` into a complete MP4. Generate 16 kHz mono PCM16 WAV. Save every
matching platform subtitle as original JSON plus deterministic VTT/TXT. Promote staging and mark the
database job done only after the whole bundle passes audit.

Migrate old standalone roots with `scripts/migrate_video_bundles.py`; it is dry-run by default and
mutates only with `--apply`. Then run `python scripts/audit_media_queue.py`.

## Caption provenance

- Use `type` as the primary generation field and retain raw enums/rule version.
- Preserve `ai_type` as a separate translation axis; do not use it alone to claim ASR generation.
- Use manual/platform_manual, automatic/platform_auto or unknown/platform_unknown.
- `need_login_subtitle=true` with no visible tracks is `auth_required`, not no subtitles.
- Keep not-provided, auth-required, no-matching-language, unknown and external failure distinct.
- Platform manual describes the CC channel and author evidence, not guaranteed human verification.
- Automatic captions do not imply AI-generated media.
- Persist `caption.inventory_attempts` only as sanitized evidence: counts,
  classification fields, URL value/form/host and rejection reason. Never retain
  URL path/query/fragment, credentials, Cookie, proxy values or signatures.
- Refresh player inventory at most once, and only when the first response is
  `provided` but yields no usable selected track. Preserve the first invalid
  result if the refresh is empty.

For completed integrated bundles, use `scripts/backfill_bilibili_captions.py`
for caption-only repair. Preview without `--apply` first. Applying requires its
SQLite backup, exact job lock, atomic sidecar and rollback guards, and must never
redownload or rewrite MP4/WAV.
Before mutation, recompute the existing disk closure and require it to equal the
saved SQLite size/hash. Keep ffprobe and large hashing outside the SQLite writer
transaction; use a short compare-and-swap update with all old identity/closure
values. BaseException recovery is best effort and cannot make SIGKILL or power
loss atomic across the filesystem and SQLite.

## Credentials and rights

Do not use an ambient or browser Bilibili Cookie. Only an intentionally supplied lawful session with
the explicit `--allow-bilibili-cookie` gate may be
sent, and only to `api.bilibili.com`; never persist, print or forward it to CDN/ffmpeg. Keep rights at
`needs_review` unless independent evidence clears the intended use.

## Completion evidence

Report exact BV/CID/part, queue job, output path, bytes, MP4/WAV ffprobe results, caption availability
and manual/automatic/unknown counts, rights/AI/speaker-review state, SHA-256 closure, audit result and
remaining staging. Synthetic tests never prove real caption coverage.
