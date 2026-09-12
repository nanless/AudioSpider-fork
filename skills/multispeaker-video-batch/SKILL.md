---
name: multispeaker-video-batch
description: Validate, collect, download, OCR, resume, and exactly audit AudioSpider's fixed cross-platform multispeaker video batches. Use when a request needs the six Bilibili/YouTube by screen/interview/forum quota cells, complete videos, conservative candidate evidence, and batch-specific completion accounting.
---

# Multispeaker Video Batch

Operate the deployed AudioSpider repository on `dev_L4_1gpus`; keep the formal route:

```text
batch manifests -> collect.py -> audiospider.db -> main.py -> downloads/<source>/<dataset_category>/
```

For the current `multispeaker-video-100-20260912` contract, require exactly six cells:
Bilibili Chinese and YouTube English, each with `影视=20`, `访谈=20`, and `会议论坛=10`.
Every produced artifact is complete platform media: a complete Bilibili part (a manifest parent may
expand to its bounded parts) or a complete YouTube parent, never an assistant-generated clip.
Same-language platform captions are best effort; genuine absence neither discards the video nor
justifies fabricated text.

Treat manifest entries as candidates until media review. Preserve `speaker_count=null`,
`speaker_count_status=needs_review`, and `candidate_metadata.*.status=candidate_unverified`; titles,
guest lists, thumbnails, or search queries do not prove a speaker count, language, rights, or AI origin.

Before any batch operation, read [references/runbook.md](references/runbook.md). It defines the exact
manifest files, current commands, single-writer gate, Bilibili formal-manifest route, YouTube profiles,
OCR fallback, credential/proxy boundaries, and manifest-to-database-to-disk reconciliation.

Do not announce completion from process exit, whole-database totals, or directory counts. Require the
exact batch audit plus bundle validation. Every new-batch download or failed-row retry must include
`--batch-id multispeaker-video-100-20260912`; source/category are additional bounds, not substitutes.
