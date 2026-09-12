---
name: youtube-caption-dataset
description: Collect, download, resume, audit, and document complete YouTube parent-video bundles with strict or best-effort same-language platform captions through AudioSpider's unified SQLite pipeline on dev_L4_1gpus. Use for long interviews, screen media, caption-missing retention, or YouTube provenance; clips are never the default.
---

# YouTube Complete Parent Video

Work in `/root/code/github_repos/AudioSpider-fork` on `dev_L4_1gpus`, using the `audiospider`
Conda environment.

## Route

The formal route is:

```text
collect.py -> audiospider.db -> main.py -> downloads/youtube/<category>/<source_id>/<job_key>/
```

Collection inspects the configured manifest and writes a bounded complete-parent `video_bundle` job;
`main.py` performs the transfer. Use `collect.py --spiders youtube` and
`main.py --source youtube --artifact-kind video_bundle`. `AUDIOSPIDER_YOUTUBE_MANIFEST` and
`AUDIOSPIDER_YOUTUBE_MAX_ITEMS` select the manifest and bound the current collection. Treat
`youtube_dataset.py` as a compatibility, repair and audit tool rather than the formal entry path.

Before acting, read `docs/DOWNLOAD-GUIDE.md`, `docs/architecture.md`, and
`docs/SIDECAR-SCHEMA.md` in the live repository.
For exact batches or real downloads, also read [references/runbook.md](references/runbook.md).

## Parent-only contract

- Accept canonical credential-free single-video URLs; reject playlists, live/upcoming media and
  unbounded source lists.
- Always save the complete parent MP4, complete 16 kHz mono PCM16 WAV and sidecar. Save platform
  VTT/TXT only when a matching platform track exists; never create placeholder or local-ASR text.
- Do not generate clips by default. A clip requires an explicit separate user request, an explicit
  authorization gate, and a parent-hash reference; it never replaces the parent.
- Use `youtube_screen_parents` for complete screen-media entries, `youtube_interviews` for complete
  interview/roundtable parents, and `youtube_conference_forums` for complete conference/forum parents.
  The legacy `youtube_screen_clips` profile remains compatible but is not an exact parent-video batch
  profile and must never be selected merely because the category is `影视`.
- `dataset_category` and `content_kind` are controlled semantic fields, independent of profile:
  `影视/screen_media`, `访谈/interview_roundtable`, `会议论坛/conference_forum`. Preserve
  arbitrary bounded `candidate_metadata` through manifest normalization, queue metadata and sidecar.
- Keep signed yt-dlp media/caption URLs and full format dictionaries out of persistent metadata.
- Mark done only after the policy-dependent closure passes: strict jobs require MP4/WAV/caption/TXT/
  sidecar; best-effort captionless jobs require MP4/WAV/sidecar with zero fake caption files.
- Metadata inspection and download run in killable child processes with explicit hard timeouts;
  preserve staging for safe resume when a transfer times out.
- Migrate old standalone roots with default-dry-run `scripts/migrate_video_bundles.py`; only `--apply`
  moves and registers bundles.
- Run `python scripts/audit_media_queue.py` to reconcile every done row with its bundle and detect orphans.

## Language and caption provenance

- Keep content language separate from caption language.
- Enforce native-language-family selection: English to English; Chinese/Mandarin/Cantonese to Chinese
  or Cantonese; other languages to their primary family. Never use another language as fallback.
- A manifest that omits `require_caption` remains strict (`true`). When the user explicitly says
  captions are best effort or captionless videos should be retained, set it to `false`.
- In best-effort mode, no platform tracks is `caption.status=missing`; tracks exist but none match the
  content language is `caption.status=no_matching_language`. Both retain the full parent without
  VTT/TXT. Network/API/parser errors are failures, never reclassified as missing captions.
- Prefer platform manual captions when available, but preserve automatic captions as platform_auto.
- Keep the implemented manual/platform_manual or automatic/platform_auto pair, translation status and
  selection rule in the sidecar; do not invent an accepted downloaded `unknown` pair.
- Platform captions are source text, not newly generated ASR.
- Caption kind does not establish media AI generation.

## Rights and review

When `dev_L4_1gpus` cannot reach YouTube directly and the user authorizes a local transport path,
terminate an SSH reverse forward at a temporary local HTTP CONNECT proxy whose allow-list is limited
to YouTube, Googlevideo and the static/API suffixes yt-dlp actually needs. Before collecting, verify a
YouTube request succeeds through the server loopback port and a non-allow-listed request is rejected
with 403. Run only the YouTube-scoped `collect.py` or `main.py` process with uppercase and lowercase
`HTTP_PROXY`/`HTTPS_PROXY`, unset `ALL_PROXY`/`all_proxy`, and keep `NO_PROXY` limited to loopback.
Never put credentials in the proxy URL or persist proxy endpoints/signed URLs in SQLite, sidecars or
logs. Keep the proxy and tunnel alive for the whole batch, then stop them and remove temporary scripts.

Public visibility does not clear download, training or redistribution rights. Keep rights at
`needs_review` without evidence. Keep unreviewed speaker counts null/needs_review and media AI status
unknown unless directly supported.

Candidate search metadata remains `candidate_unverified`: titles, guest lists and thumbnails may select
an item for inspection but do not prove English speech, multiple audible speakers, exact speaker count,
rights, or media AI status.

Every download and `--retry-failed` run for the exact multispeaker batch must include
`--batch-id multispeaker-video-100-20260912`; source/category/language remain additional filters.

## Completion evidence

Report video ID, queue job, full-parent duration, output path/bytes, MP4/WAV ffprobe, selected native
caption language and kind, SHA-256 closure, rights/AI/speaker-review state, audit failures and staging.
Reconcile exactly the manifest job keys and distinct parent video IDs requested; every intended row must
be `done` and pass both bundle and unified queue audits. Do not count archived/historical clips, unrelated
whole-database totals or worker exit as completion, and do not use synthetic fixtures as real caption evidence.
