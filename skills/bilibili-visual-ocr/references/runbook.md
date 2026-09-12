# Bilibili visual OCR runbook

## Preflight

```bash
cd /root/code/github_repos/AudioSpider-fork
bash scripts/bootstrap_ocr_conda.sh
/root/miniforge3/envs/audiospider/bin/python doctor.py --ocr
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --help
```

安装脚本会显式从 PaddlePaddle 的官方 ModelScope 仓库预热固定的检测、识别模型；预热未成功时不要启动长批次。

Confirm the download root is `downloads/bilibili/<category>/<source_id>/<job_key>/`,
the reverse proxy is limited to approved Bilibili hosts, and no other repair owns
the same job locks.

For `multispeaker-video-100-20260912`, derive the OCR candidate set from the exact Bilibili formal
manifest plus SQLite rows for that `batch_id`; never use the whole Bilibili download tree as the batch.
Only completed rows with `visual_ocr_fallback=true`, the manifest-bound profile, and no valid
same-language platform track are eligible. Keep six-cell accounting unchanged: OCR is a repair result,
not a new sample or category.
The formal downloader and `--retry-failed` runs that create these bundles must include
`--batch-id multispeaker-video-100-20260912`; visual OCR then narrows further by exact `--job-key`.

## Safe order

1. With an explicitly authorized login, refresh platform captions first. Pass
   the Cookie only in process memory; never print it, write it to a file, embed
   it in argv, sidecars, logs, or Git.
2. Run the OCR command without `--apply` and review its exact candidates.
3. Run one known burned-in-subtitle canary with an exact repeatable
   `--job-key`, first without `--apply`, then with `--apply --visual-ocr`.
4. Validate the canary, compare MP4/WAV SHA-256 before and after, and inspect
   JSON/VTT/TXT content manually.
5. Expand to ten videos, then to a bounded batch. Keep one OCR GPU worker.

Use `python scripts/backfill_bilibili_captions.py --help` as the source of truth
for flags. The normal OCR controls include engine, profile, ROI, sample FPS, and
minimum recognition confidence.

```bash
sqlite3 -header -column audiospider.db \
  "SELECT source_id,job_key,bundle_path FROM audio_urls WHERE source='bilibili' AND artifact_kind='video_bundle' AND status='done' ORDER BY id DESC LIMIT 20;"
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py \
  --job-key '<exact-job-key>' --limit 1 --visual-ocr
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py \
  --job-key '<exact-job-key>' --limit 1 --visual-ocr --apply
```

The dry run does not contact Bilibili, load PaddleOCR, or write anything.
Require exactly one candidate and verify source ID, job key, and bundle path.
Keep one repair writer for the selected job. A downloader must not own the same row, and two backfills
must not race on its lock or rollback directory.

## Artifacts and audit

Successful text extraction adds:

```text
visual_ocr.json
visual_ocr.vtt
visual_ocr.txt
```

`visual_ocr.json` is canonical; VTT/TXT must rebuild deterministically from it.
When no stable text is accepted, keep only diagnostic `visual_ocr.json`.

After every batch:

```bash
/root/miniforge3/envs/audiospider/bin/python scripts/audit_media_queue.py
```

Require zero invalid bundles, zero unregistered files, zero orphan bundles, and
no leftover staging/rollback directory. Review `derived_text.visual_ocr.quality`
and spot-check both positives and negatives.

## Recovery boundary

The repair owns only `visual_ocr_*` file keys. Platform caption refresh owns
only `caption_*`. Both must preserve future unrelated assets. A failure before
the SQLite compare-and-swap must restore files and metadata; keep any rollback
directory when restoration itself is incomplete and stop for manual audit.

Do not interpret OCR confidence as calibrated correctness probability. For
production acceptance, build a manually labeled set and report presence
precision/recall, cue precision/recall, CER, timing error, false cues/minute,
real-time factor, peak VRAM, and model/package versions.

OCR never receives Cookie, browser state, proxy variables, or signed platform URLs. If the user has
explicitly authorized a platform-caption refresh, finish that separate Bilibili API step first using the
minimum Bilibili-origin allow-list in memory and only `api.bilibili.com`; clear the credential before
starting Paddle. Never send it to subtitle/media CDN, ffmpeg, Paddle, a generic subprocess, logs or disk.

After each exact batch repair, run both audits:

```bash
/root/miniforge3/envs/audiospider/bin/python scripts/audit_media_queue.py
/root/miniforge3/envs/audiospider/bin/python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --db audiospider.db
```

Keep `speaker_count=null/needs_review` and `candidate_metadata.*.status=candidate_unverified` until a
separate human review. Recognized dialogue does not establish the number or identity of speakers.
