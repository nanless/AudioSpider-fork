# YouTube 数据集 sidecar 参考

每个完整父视频 bundle 都有一个 UTF-8 `metadata.json`。只有 sidecar 最后原子写入且目录完成提升，bundle 才算完成。短片字段只为历史兼容保留，当前正式流程不生成 clip。

## 父视频字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | integer | 当前为 1 |
| `asset_type` | string | `youtube_parent` |
| `profile` | string | `youtube_interviews` 或 `youtube_screen_clips` |
| `job_key` | string | 视频 ID、profile、请求字幕语言和来源修订组成的稳定任务键 |
| `source_id` | string | 11 字符 YouTube video ID |
| `source_revision` | string | 清单声明的来源修订，默认 `current` |
| `canonical_url` | string | 不带查询签名的规范 watch URL |
| `title` / `description` | string | 平台公开标题和简介 |
| `duration_seconds` | number | 平台报告的父视频时长 |
| `channel` / `channel_id` | string | 平台频道信息 |
| `language` | string | 对白/内容语言，来自清单 `content_language` |
| `requested_languages` | array | 字幕选择优先顺序 |
| `speaker_count` | integer/null | 人工确认人数或 null |
| `speaker_count_status` | string | `verified_manual` 或 `needs_review` |
| `rights` | object | 权利状态、证据和备注 |
| `rights_cleared` | boolean | 权利状态属于准入集合且有证据时才为 true |
| `ai_generation` | object | AI 生成状态与可复核证据，不用听感冒充事实 |
| `caption` | object | 被选字幕轨及来源 |
| `files` | object | bundle 内相对路径、字节数和 SHA-256 |
| `toolchain` | object | yt-dlp、切分算法和编码 profile 版本 |
| `acquired_at` | string | UTC ISO 8601 获取时间 |

## `caption` 字段

| 字段 | 例子 | 含义 |
|---|---|---|
| `status` | `downloaded` | 字幕状态 |
| `kind` | `manual` | 人工或自动字幕 |
| `text_source` | `platform_manual` | 明确文本来源 |
| `requested_languages` | `["yue", "zh-Hant", "zh-HK", "zh"]` | 与内容语言一致的请求优先级 |
| `track_language` | `zh-Hant` | 实际下载轨道语言 |
| `source_language` | `en` | 平台声明的源语言 |
| `track_name` | `English` | 平台轨道名 |
| `track_format` | `vtt` | 最终规范格式 |
| `is_translated` | null | 是否明确为翻译轨；平台未声明时为 null |
| `translation_kind` | `unknown` | 翻译类型，未知时不猜 |
| `selected_by_rule` | `exact_requested_language` | 命中的确定性规则 |

临时字幕 URL 不进入 sidecar。字幕原文件和规范化 TXT 的 SHA-256 位于 `files.caption_vtt` 和 `files.transcript_txt`。

字幕语言硬规则：英文内容只接受英文字幕；中文、普通话或粤语内容只接受中文/粤语字幕；其他内容按主语言代码一致。跨语言字幕会被 manifest、下载后确认和 audit 拒绝。

## `files` 字段

父视频通常包含：

```json
{
  "video": {"path": "source.mp4", "bytes": 123, "sha256": "..."},
  "audio": {"path": "audio.wav", "bytes": 456, "sha256": "..."},
  "caption_vtt": {"path": "captions.en.manual.vtt", "bytes": 789, "sha256": "..."},
  "transcript_txt": {"path": "captions.en.manual.txt", "bytes": 100, "sha256": "..."}
}
```

路径必须是 bundle 内的相对路径。审计会拒绝路径逃逸、文件缺失和哈希不一致。

## 历史短片字段（当前不生成）

短片 `asset_type=youtube_screen_clip`，除通用字段外还包含：

| 字段 | 含义 |
|---|---|
| `clip_id` | 由父媒体/字幕哈希、起止毫秒和算法版本生成 |
| `parent_video_id` | 父 YouTube video ID |
| `parent_job_key` | 父任务键 |
| `parent_media_sha256` | 父视频哈希 |
| `parent_caption_sha256` | 父 VTT 哈希 |
| `start_ms` / `end_ms` | 在父视频时间轴中的整数毫秒 |
| `duration_seconds` | 派生目标时长，必须 0.418–29.888 秒 |
| `cue_count` | 合并的字幕 cue 数 |

短片说话人数默认重新标记为 `needs_review`；不能把父视频总说话人数直接当成片段的精确人数。

## 权利状态

`rights_cleared=true` 只有在 `rights.status` 属于以下集合并且存在 `evidence_url` 或 `evidence_text` 时产生：

- `licensed`
- `public_domain`
- `permission_granted`
- `creative_commons`

`needs_review`、`unknown` 或缺证据均为 false。这个机器字段用于防止候选集被误当作已授权集，但不能代替法律判断。

## AI 生成状态

`ai_generation.status` 只允许：

- `declared`：来源页面或作者明确声明为 AI 生成；
- `not_declared`：只表示检查过的来源没有声明，不等于技术上证明非 AI；
- `suspected`：存在模型检测或人工复核线索，但证据不足；
- `unknown`：没有可靠结论，默认值。

证据写在 `ai_generation.evidence` 字符串数组中。压缩伪影、机械音色、异常韵律等只能作为线索，不能单独把样本标成 `declared`。

## 兼容与迁移

读取程序必须先检查 `schema_version`，忽略未知可选字段，不应依赖 JSON 键顺序。未来算法或编码 profile 改变时，新的 clip ID 不会静默覆盖旧短片。
