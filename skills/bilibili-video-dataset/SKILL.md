---
name: bilibili-video-dataset
description: Collect, download, resume, audit, and document complete Bilibili per-part video bundles through AudioSpider's unified SQLite media pipeline on dev_L4_1gpus. Use for Bilibili video, DASH, CID, platform subtitles, burned-in subtitle visual OCR, or bundle provenance; do not route new formal work through the legacy audio-only or standalone dataset path.
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
- For an exact new-parent batch, set the `AUDIOSPIDER_BILIBILI_*` keyword, title-term, search-page,
  per-keyword, part, min/max-duration and `MAX_NEW_RECORDS` controls. The collector uses SQLite's
  parent-BV acceptance: one parent consumes one quota only when at least one of its
  rows is newly inserted, and all accepted parts remain intact. Duplicate parent BVs do not consume the quota. Use
  `MAX_PAGES_PER_VIDEO=1` only when the request means one complete P1 video per BV.
- Use `AUDIOSPIDER_BILIBILI_CATEGORY` when branded search phrases would otherwise map the same
  requested dataset into several categories; for a Chinese interview batch, set it to `访谈` so
  `main.py --category 访谈` selects the exact queue slice.
- Treat configured `content_language=zh` and title/search filtering as selection evidence, not acoustic
  proof of spoken language. Report this limitation unless a separate authorized language audit ran.
- Never use an example manifest as the user's target.

## Artifact closure

Download video and audio DASH representations separately, bind resume to a signed-URL-free descriptor,
and merge explicit `v:0` plus `a:0` into a complete MP4. Generate 16 kHz mono PCM16 WAV. Save every
matching valid platform subtitle as credential-sanitized platform JSON plus deterministic VTT/TXT.
Classify each track against the actual MP4 duration: quarantine an overlong/mismatched track as
`.rejected.json` with numeric evidence and do not emit VTT/TXT for it. Promote staging and mark the
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
- Persist `caption.inventory_attempts` only as sanitized evidence: counts, classification fields, URL
  value/form/host and rejection reason. Never retain URL path/query/fragment, credentials, Cookie,
  proxy values or signatures.
- Refresh player inventory at most once, and only when the first response is `provided` but yields no
  usable selected track. Preserve the first invalid result if the refresh is empty.
- A mixed valid/rejected result stays `downloaded` with `payload_status=partial`. If every selected track
  is rejected, best-effort keeps MP4/WAV and uses `invalid_timeline`; strict caption policy fails.
- Treat `.rejected.json` as evidence, never as a usable transcript. It preserves cue content and safe
  platform structure after removing signed transport URLs and credential material.
- Caption-only repair excludes `invalid_timeline` by default. Use `--retry-invalid-timeline` only when the
  platform track may have changed; the repair must replace prior caption files through its rollback-safe
  sidecar/SQLite compare-and-swap path.
- Before mutation, recompute the old disk closure and require it to equal SQLite's saved size/hash. Keep
  ffprobe and large hashing outside the writer transaction. Recovery covers catchable exceptions but
  cannot promise filesystem/SQLite atomicity across `SIGKILL`, power loss or storage failure.
- Visual OCR is the explicit `--visual-ocr` fallback in `scripts/backfill_bilibili_captions.py`, not a
  new downloader or queue. Platform tracks remain first; OCR uses the completed MP4 only when no valid
  same-language platform track exists.
- Mark it `visual_ocr`, automatic extraction, unknown authorship and unreviewed. Bind input hash,
  Paddle engine/model/profile, ROI, fps time basis/uncertainty, observations, confidence and quality
  counters. Never overwrite/relabel platform provenance.
- The committed statuses are `downloaded` and `no_stable_text_detected`; backend failures remain repair
  failures. Static overlays, danmaku, lower thirds, tiny/blurred/animated/occluded and bilingual text are
  known failure modes; absence of stable text is not proof of no burned-in subtitles.

## Credentials and rights

When the server cannot reach Bilibili directly and the user has authorized a local transport path,
use the source-scoped `AUDIOSPIDER_BILIBILI_PROXY=http://127.0.0.1:<port>` entry on both
`collect.py` and `main.py`. The remote port must terminate at a temporary local allow-list proxy that
permits only the Bilibili API, subtitle and media-CDN suffixes required by the job. Verify both a
Bilibili request succeeds and a non-allow-listed request returns 403 before running the queue.
Do not enable aiohttp `trust_env`, do not use ambient `HTTP_PROXY`/`HTTPS_PROXY` for Bilibili, and do
not write the proxy endpoint to SQLite, sidecars or logs. Keep tunnel, proxy and cookie lifetimes
bounded to the active batch and remove temporary bridge scripts after the processes finish.

Do not use an ambient or browser Bilibili Cookie without explicit user authorization. An intentionally
supplied lawful session must use the `--allow-bilibili-cookie` gate, remain process-local/in memory,
and be sent only to `api.bilibili.com`; never persist, print or forward it to CDN/ffmpeg. Keep rights at
`needs_review` unless independent evidence clears the intended use.

If the user authorizes their current Edge Bilibili session, extract only the minimum Bilibili-origin
allow-list through stdin or a one-shot memory bridge; never copy the profile or inspect other-origin
cookies. OCR itself uses local pixels and must not receive the Cookie.

## Completion evidence

Report exact BV/CID/part, queue job, output path, bytes, MP4/WAV ffprobe results, caption availability
and manual/automatic/unknown counts, rights/AI/speaker-review state, SHA-256 closure, audit result and
remaining staging. For a requested batch, reconcile its baseline/job keys and distinct `source_id` values;
every target must be `done`, pass `validate_bundle`, and pass `scripts/audit_media_queue.py`. A worker exit
or whole-database count is not completion. Synthetic tests never prove real caption coverage.
For OCR also report platform status separately, OCR status/profile/model, cue precision/recall, CER,
timing p95, false cues/minute, RTF, GPU peak and human-review state.
