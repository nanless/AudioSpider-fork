# AudioSpider Sidecar Schema

## 状态与范围

统一媒体队列已经实现，但“编排统一”不等于“所有产物强制同一 JSON 形状”。当前
验证器接受三种实际 sidecar：

| artifact | schema | 文件位置 |
|---|---:|---|
| 普通音频 | `schema_version=2` | 与音频同 stem 的 `.json` |
| B站完整分P | `schema_version=1`, `asset_type=bilibili_parent` | bundle 内 `metadata.json` |
| YouTube完整母视频 | `schema_version=1`, `asset_type=youtube_parent` | bundle 内 `metadata.json` |

SQLite `audio_urls.artifact_kind` 区分 `audio` 与 `video_bundle`；视频行还使用
`bundle_path` 指向完整目录，`local_path` 指向其中 `source.mp4`。平台 sidecar 保留各自
已经过测试的结构，公共语义通过下列字段和审计规则对齐。

## 设计原则

1. sidecar 描述事实和证据，不替代数据库任务状态；
2. 普通音频 sidecar 与音频同 stem；视频 sidecar 固定为 bundle 内 `metadata.json`；
3. 签名 URL、Cookie 和凭据不持久化；
4. 所有 payload 使用相对路径、字节数和 SHA-256 闭包；
5. 字幕来源、字幕语言、内容语言、媒体 AI 来源、rights 分开；
6. 未知必须显式保存为 unknown，不能从缺字段推断否定结论。

## 公共语义

| 字段 | 必需 | 规则 |
|---|---:|---|
| `schema_version` | 是 | 正整数；验证器按已知版本分派 |
| `artifact_kind` | 数据库 | `audio` 或 `video_bundle` |
| `job_key` | 视频必需 | 不可变任务键；同 URL 的不同策略/修订不会互相覆盖 |
| `source/source_id` | 是 | source 受控；source ID稳定且无签名 query |
| `title/category` | 是 | 展示字段，不能作为唯一身份或未净化路径 |
| `language/content_language` | 是 | 未知用空值或 `und`，按具体 schema |
| `canonical_url/webpage_url` | 可选 | 无凭据、query 脱敏的公开页面 URL |
| `source_metadata/background_metadata` | 是 | 版本化平台/RSS 特有事实 |
| `caption/captions` | 视频必需 | 即使无轨也保存可用性状态 |
| `rights` | 视频必需 | 默认 `needs_review` |
| `ai_generation` | 视频必需 | 与 caption kind 独立 |
| `speaker_count/status` | 视频必需 | 未核验为 null/`needs_review` |
| `files` | 视频必需 | bundle 所有正式 payload 闭包 |
| `toolchain` | 视频必需 | ffmpeg/yt-dlp/schema profile |
| `acquired_at` | 是 | 带时区 ISO 8601 |

## 普通音频 sidecar

普通音频当前实际 sidecar 示例：

```json
{
  "schema_version": 2,
  "title": "节目标题",
  "source": "podcast_rss",
  "source_id": "stable-source-id",
  "original_url": "https://example.test/audio.mp3",
  "original_url_query_redacted": true,
  "webpage_url": "https://example.test/episode",
  "description": "...",
  "author": "...",
  "cover_url": "https://example.test/cover.jpg",
  "file_format": "mp3",
  "file_size": 123456,
  "duration": 1800,
  "language": "zh",
  "category": "播客",
  "speaker": "节目名",
  "published_at": "2026-09-10T00:00:00+00:00",
  "content_hash": "...",
  "content_hash_algorithm": "sha256",
  "background_metadata": {},
  "background_files": {},
  "acquired_at": "2026-09-10T00:00:00+00:00"
}
```

主音频的路径和状态由数据库 `local_path`/`artifact_kind=audio` 记录；sidecar 的
`content_hash` 是最终音频 SHA-256。背景简介、封面、RSS transcript、章节和公版
原文进入 `background_files`，并标记 `text_source=platform/rss`，不能称为 ASR。

## 视频 bundle sidecar

`artifact_kind=video_bundle` 时，正式目录至少包含 MP4、WAV 和 metadata：

```json
{
  "schema_version": 1,
  "asset_type": "bilibili_parent",
  "source": "bilibili",
  "source_id": "BVxxxx_p1",
  "job_key": "...",
  "content_language": "zh",
  "files": {
    "video": {
      "path": "source.mp4",
      "bytes": 1000000,
      "sha256": "..."
    },
    "audio": {
      "path": "audio.wav",
      "bytes": 200000,
      "sha256": "..."
    }
  },
  "media": {
    "video_stream_count": 1,
    "audio_stream_count": 1,
    "width": 1280,
    "height": 720,
    "duration_seconds": 600.0
  },
  "audio": {
    "audio_codec": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "duration_seconds": 600.0
  }
}
```

对 B站还要保留 BV、aid、CID、part 和无签名表示层 descriptor；对 YouTube 保留 video
ID、profile 与 yt-dlp 版本，但不能保存完整 format 字典或临时媒体 URL。

## 字幕结构

```json
{
  "caption": {
    "status": "downloaded",
    "need_login_subtitle": false,
    "requested_languages": ["zh-Hans", "zh-CN", "yue"],
    "track_count": 1,
    "tracks": [
      {
        "id": "123",
        "language": "zh-Hans",
        "kind": "automatic",
        "text_source": "platform_auto",
        "translation_kind": "normal",
        "selected_by_rule": "bilibili_type_ai",
        "rule_version": "bilibili-caption-provenance-v1",
        "track_type": 1,
        "ai_type": 0,
        "ai_status": 2,
        "cue_count": 321,
        "files": {
          "json": "caption_0_json",
          "vtt": "caption_0_vtt",
          "txt": "caption_0_txt"
        }
      }
    ]
  }
}
```

允许的 availability：

| 状态 | 含义 |
|---|---|
| `downloaded` | 至少一条字幕闭包完成 |
| `missing` | 平台成功响应，但没有任何可用字幕轨 |
| `auth_required` | 平台明确要求登录才能判断或取得 |
| `not_provided_publicly` | 成功公开响应明确无轨 |
| `no_matching_language` | 有轨但没有满足同语言策略的轨 |
| `unknown` | 字段不足，不能下结论 |

HTTP/API 错误不是上述任一空字幕状态，应让任务失败或记录显式 external error。

YouTube 每条任务还保存 `caption.required`。`required=false` 且状态为 `missing` 或
`no_matching_language` 时，`kind/text_source/track_language` 必须为 null，文件闭包精确为
MP4、WAV 和 sidecar；不允许用空文件伪造字幕。

`kind` 与 `text_source` 必须成对：

```text
manual    <-> platform_manual
automatic <-> platform_auto
unknown   <-> platform_unknown
```

平台 manual 只代表 CC/作者证据，不保证逐字人工制作或审核。自动字幕也不影响
`ai_generation.status`。

## 字幕语言策略

- `content_language` 与 `caption.language` 分开保存；
- 英语内容只接受英语字幕；
- 中文、普通话和粤语归入中文文本族；
- 日语、韩语等匹配自己的主要语言代码；
- 不允许中文视频使用英文 fallback；
- 自动翻译必须保存 `translation_kind=automatic_translation`；
- 平台未说明翻译关系时保存 unknown，不能写 false。

## Rights 与 AI 来源

```json
{
  "rights": {
    "status": "needs_review",
    "evidence_url": "",
    "evidence_text": ""
  },
  "ai_generation": {
    "status": "unknown",
    "evidence": []
  }
}
```

rights 只有在证据可复核时才可升级为 licensed、permission_granted、public_domain 或
creative_commons。公开页面、下载成功、B站 copyright 枚举、YouTube频道名称都不是
充分清权证据。

AI 来源允许：

- `declared`：来源明确声明；
- `not_declared`：来源明确声明非 AI，且证据可复核；
- `suspected`：听感/模型/间接线索；
- `unknown`：没有充分证据。

caption automatic 永远不能自动设置 media AI declared。

## 文件闭包

每个 `files` 记录：

```json
{
  "path": "relative/path",
  "bytes": 123,
  "sha256": "64-hex"
}
```

验证器必须拒绝：

- 绝对路径、`..`、软链接外逸；
- 文件缺失或多出未登记 payload；
- bytes/hash 不一致；
- signed URL/Cookie/credential；
- MP4 缺视频或音频流；
- WAV 不是 16 kHz mono PCM16；
- 字幕 JSON 无法确定性重建 VTT/TXT；
- `kind/text_source` 交叉；
- 内容语言和字幕语言不匹配；
- `metadata.json` 存在但 staging 未完成。

## 版本与迁移

- 不原地假装旧 sidecar 已是新版本；
- validator 按 schema version 分派；
- repair 只重建可确定派生物，不伪造来源字段；
- `scripts/migrate_video_bundles.py` 默认 dry-run 并先 audit；`--apply` 先备份数据库，
  再移动和登记为 done；
- 迁移报告必须列 rich/partial/unresolved 或同等证据分层；
- 不用非空 JSON shell 充当完整元数据。

具体迁移顺序见[实施计划](plans/2026-09-10-unified-media-pipeline.md)。
