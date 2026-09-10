---
name: youtube-caption-dataset
description: Collect, download, resume, audit, and document complete captioned YouTube parent videos through AudioSpider's unified SQLite media pipeline on dev_L4_1gpus. Use for long interviews, complete screen-media videos, native-language platform captions, or YouTube bundle provenance; clips are never the default.
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

## Parent-only contract

- Accept canonical credential-free single-video URLs; reject playlists, live/upcoming media and
  unbounded source lists.
- Save the complete parent MP4, complete 16 kHz mono PCM16 WAV, platform caption, TXT and sidecar.
- Do not generate clips by default. A clip requires an explicit separate user request, an explicit
  authorization gate, and a parent-hash reference; it never replaces the parent.
- Keep signed yt-dlp media/caption URLs and full format dictionaries out of persistent metadata.
- Mark done only after MP4/WAV/caption/sidecar/hash audit and staging cleanup.
- Metadata inspection and download run in killable child processes with explicit hard timeouts;
  preserve staging for safe resume when a transfer times out.
- Migrate old standalone roots with default-dry-run `scripts/migrate_video_bundles.py`; only `--apply`
  moves and registers bundles.
- Run `python scripts/audit_media_queue.py` to reconcile every done row with its bundle and detect orphans.

## Language and caption provenance

- Keep content language separate from caption language.
- Enforce native-language-family captions: English to English; Chinese/Mandarin/Cantonese to Chinese or
  Cantonese; other languages to their primary family. Never use English fallback for Chinese content.
- Prefer platform manual captions when available, but preserve automatic captions as platform_auto.
- Keep manual/automatic/unknown, translation status and selection rule in the sidecar.
- Platform captions are source text, not newly generated ASR.
- Caption kind does not establish media AI generation.

## Rights and review

Public visibility does not clear download, training or redistribution rights. Keep rights at
`needs_review` without evidence. Keep unreviewed speaker counts null/needs_review and media AI status
unknown unless directly supported.

## Completion evidence

Report video ID, queue job, full-parent duration, output path/bytes, MP4/WAV ffprobe, selected native
caption language and kind, SHA-256 closure, rights/AI/speaker-review state, audit failures and staging.
Do not count archived/historical clips as current formal parents or use synthetic fixtures as real
caption evidence.
