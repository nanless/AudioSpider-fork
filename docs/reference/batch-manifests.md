# 批次 Manifest 参考

> 状态：当前实现参考。三个配置、两平台 manifest 校验、B站清单采集、
> `main.py --batch-id` 和批次审计均已实现；候选数据尚未因此自动完成下载或人工核验。

本文定义跨平台 100 条多人视频批次的三个 JSON 文件。它们只决定“处理哪些目标”和
“如何验收”，不直接下载媒体。

## 1. 文件关系

```text
config/multispeaker_video_100_20260912.batch.json
├── config/bilibili_multispeaker_50_20260912.json
└── config/youtube_multispeaker_50_20260912.json
```

批次总控固定全局策略和六格配额，平台子 manifest 固定每个视频。正式采集仍由
`collect.py --spiders bilibili` 和 `collect.py --spiders youtube` 完成。

## 2. 受控枚举

### 平台

```text
bilibili
youtube
```

### 内容分类

| `content_kind` | `category` | 每个平台目标 |
|---|---|---:|
| `screen_media` | `影视` | 20 |
| `interview_roundtable` | `访谈` | 20 |
| `conference_forum` | `会议论坛` | 10 |

`content_kind` 是机器字段，`category` 是目录和展示字段。二者必须按表对应，不能使用自由
文本近义词，否则六格统计会分裂。

### 语言

- B站条目：`content_language=zh`，字幕语言只允许中文同族；
- YouTube 条目：`content_language=en`，字幕语言只允许英文同族。

### 字幕策略

本批固定 `require_caption=false`。这表示同语言字幕 best effort，不表示不检查字幕。

## 3. 批次总控结构

示例：

```json
{
  "schema_version": 1,
  "batch_id": "multispeaker-video-100-20260912",
  "manifests": {
    "bilibili": "config/bilibili_multispeaker_50_20260912.json",
    "youtube": "config/youtube_multispeaker_50_20260912.json"
  },
  "target_counts": {
    "bilibili": {"影视": 20, "访谈": 20, "会议论坛": 10, "total": 50},
    "youtube": {"影视": 20, "访谈": 20, "会议论坛": 10, "total": 50},
    "total": 100
  },
  "policies": {
    "complete_platform_video": true,
    "assistant_generated_clips": false,
    "same_language_platform_captions": "best_effort",
    "speaker_count": "candidate_unverified until manual listening review"
  }
}
```

### 总控必需规则

- `schema_version` 是已支持的正整数；
- `batch_id` 只含安全 ASCII 字符，且发布后不可原地复用为另一批目标；
- `manifests` 必须分别指向 B站和 YouTube 子清单；
- 两个平台目标都是 50，六格合计是 100；
- 子 manifest 使用仓库内相对路径，不能绝对路径或包含 `..`；
- 总控文件不得包含 Cookie、token、代理地址或媒体签名 URL。

## 4. 两个平台共有的条目字段

| 字段 | 类型 | 必需 | 规则 |
|---|---|---:|---|
| `source_revision` | string | 是 | 本批固定为 batch ID，并参与任务身份 |
| `batch_id` | string | 是 | 固定 `multispeaker-video-100-20260912`，供精确领取与对账 |
| `content_kind` | enum | 是 | 三类机器值之一 |
| `dataset_category` | enum | 是 | 与 content_kind 精确对应 |
| `content_language` | string | 是 | B站 `zh`，YouTube `en` |
| `languages` | array | 是 | 只列同语言族字幕优先级 |
| `require_caption` | boolean | 是 | 本批固定为 false |
| `speaker_count` | integer/null | 是 | 当前候选为 null；人工核验后才可写 profile 范围内整数 |
| `speaker_count_status` | string | 是 | null 必须配 `needs_review`；整数必须配 `verified_manual` |
| `rights` | object | 是 | 默认 needs_review，证据必须可复核 |
| `ai_generation` | object | 是 | 默认 unknown，与字幕类型独立 |
| `program` | string | 是 | 节目、作品或活动名；不是身份键 |
| `candidate_metadata` | object | 是 | 发现时信息；多人/语言证据为 `candidate_unverified` |

当前三个配置定义的是候选批次，因此合法且预期使用
`speaker_count=null/needs_review`。这允许采集和下载候选，但不能把它宣传成已人工确认的
多人数据。人工复核若要改变清单，必须更新证据并重新采集新的任务身份。

## 5. 说话人数证据

候选清单的初始结构：

```json
{
  "speaker_count": null,
  "speaker_count_status": "needs_review",
  "candidate_metadata": {
    "multi_speaker_evidence": {
      "status": "candidate_unverified",
      "evidence_kind": "访谈、演员问答或论坛等发现线索",
      "minimum_possible_speakers": 2,
      "limitations": [
        "标题或参与者信息不能证明实际发声人数。",
        "必须听取媒体后才能填写最终 speaker_count。"
      ]
    }
  }
}
```

人工听审完成后，才可升级为：

```json
{
  "speaker_count": 4,
  "speaker_count_status": "verified_manual",
  "speaker_count_evidence": {
    "method": "manual_full_media_review",
    "verified_at": "2026-09-12",
    "note": "确认四位可区分说话者；不声明实名身份或逐句归属"
  }
}
```

规则：

- `speaker_count` 不能由标题、人名数量、缩略图人脸数或字幕 speaker label 自动推断；
- `verified_manual` 只能在人工核对实际媒体后使用；
- `note` 不保存浏览器账户、真实 Cookie 或无关个人信息；
- 如果只能确定“至少两人”而不能确定总人数，当前整数合同不适合，必须继续复核或替换；
- 说话人数不是 diarization 标注，不能声称每个时间段已有说话人身份。

## 6. B站子 manifest

实际顶层：

```json
{
  "schema_version": 2,
  "batch_id": "multispeaker-video-100-20260912",
  "platform": "bilibili",
  "items": []
}
```

单项示例：

```json
{
  "bvid": "BVxxxxxxxxxx",
  "parts": [],
  "max_parts": 200,
  "max_height": 720,
  "max_duration_seconds": 14400,
  "source_revision": "multispeaker-video-100-20260912",
  "content_kind": "interview_roundtable",
  "dataset_category": "访谈",
  "content_language": "zh",
  "languages": ["zh", "zh-Hans", "zh-Hant", "yue", "ai-zh"],
  "require_caption": false,
  "speaker_count": null,
  "speaker_count_status": "needs_review",
  "program": "节目名称",
  "rights": {
    "status": "needs_review",
    "evidence_url": "https://www.bilibili.com/video/BVxxxxxxxxxx"
  },
  "ai_generation": {
    "status": "unknown",
    "evidence": []
  },
  "candidate_metadata": {
    "multi_speaker_evidence": {
      "status": "candidate_unverified",
      "evidence_kind": "interview_or_roundtable_title",
      "minimum_possible_speakers": 2,
      "limitations": ["标题线索不能证明实际发声人数"]
    }
  }
}
```

### B站特有规则

- `bvid` 必须是规范 BV ID；
- `parts=[]` 表示接收该 BV 当前可见的全部分 P，但最多处理 `max_parts`；也支持显式分 P 列表；
- 分 P 的实际 CID 在 collect 阶段解析，并绑定 `job_key`；
- 每个接收的分 P 都保存完整视频，不能下载后再切片；因此一个 BV 父目标可能生成多条完整
  分 P 任务，批次配额仍按父 BV 对账；
- 中文同族平台字幕可有多轨，每轨独立保存来源；
- `need_login_subtitle=true` 是 `auth_required`，不是“明确无字幕”；
- 视觉 OCR 不属于 manifest 平台字幕，也不改变 `caption.status`；
- 设置 `AUDIOSPIDER_BILIBILI_MANIFEST` 后，正式 Bilibili Spider 使用清单模式，并优先于
  搜索模式；清单读取或校验失败会直接报错，不会悄悄退回搜索。

## 7. YouTube 子 manifest

实际顶层：

```json
{
  "schema_version": 2,
  "batch_id": "multispeaker-video-100-20260912",
  "platform": "youtube",
  "items": []
}
```

单项示例：

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "profile": "youtube_conference_forums",
  "source_revision": "multispeaker-video-100-20260912",
  "content_kind": "conference_forum",
  "dataset_category": "会议论坛",
  "content_language": "en",
  "languages": ["en", "en-US", "en-GB"],
  "require_caption": false,
  "speaker_count": null,
  "speaker_count_status": "needs_review",
  "program": "Event or program name",
  "rights": {
    "status": "needs_review",
    "evidence_url": "https://www.youtube.com/watch?v=VIDEO_ID"
  },
  "ai_generation": {
    "status": "unknown",
    "evidence": []
  },
  "candidate_metadata": {
    "multi_speaker_evidence": {
      "status": "candidate_unverified",
      "evidence_kind": "panel_or_forum_title",
      "minimum_possible_speakers": 2,
      "limitations": ["节目描述不能证明实际发声人数"]
    }
  }
}
```

### YouTube 特有规则

- URL 必须是无凭据的规范单视频 watch URL；拒绝 playlist、直播和待开播；
- `screen_media` 使用 `youtube_screen_parents`；保存平台上的完整父视频，不生成派生 clip；
- `interview_roundtable` 使用 `youtube_interviews`；`conference_forum` 使用
  `youtube_conference_forums`，且 `dataset_category` 必须匹配；
- 英文内容只选择英文同族字幕；优先平台人工轨，也可保留明确自动轨；
- `require_caption=false` 时，`missing` 或 `no_matching_language` 仍可完成 MP4/WAV/metadata；
- yt-dlp 的临时媒体和字幕 URL、完整 formats 字典不得持久化。

## 8. Rights 字段

保守默认：

```json
{
  "rights": {
    "status": "needs_review",
    "evidence_url": "规范公开视频页面",
    "note": "公开可访问不代表允许训练、再分发或商业使用"
  }
}
```

只有存在可复核许可、明确授权或公版证据时，才能使用 `licensed`、
`permission_granted`、`creative_commons` 或 `public_domain`。页面可播放、下载成功、作者名或
平台版权枚举本身都不是充分清权证据。

## 9. AI 生成状态

```json
{
  "ai_generation": {
    "status": "unknown",
    "evidence": []
  }
}
```

允许状态：

- `declared`：来源明确声明媒体由 AI 生成；
- `not_declared`：仅当项目对该词有明确、受验证定义时使用，不能把“没看到声明”当证明；
- `suspected`：模型或人工观察只有线索；
- `unknown`：没有可靠结论，本批默认值。

字幕为 automatic 不能自动改变媒体 AI 状态。

## 10. 字幕状态与文件闭包

平台字幕必须与内容语言同族，并保留来源：

```text
manual    <-> platform_manual
automatic <-> platform_auto
unknown   <-> platform_unknown
```

B站需要区分 `auth_required`、`not_provided_publicly`、`no_matching_language`、
`invalid_track_inventory`、`invalid_timeline` 和 `unknown`。YouTube best-effort 无轨为
`missing`，有轨但无同语言轨为 `no_matching_language`。网络错误不能写成这些正常缺失状态。

无平台字幕时不得创建空字幕文件。B站 OCR 文件只能登记在 `derived_text.visual_ocr`，并
绑定输入视频哈希、引擎、模型、profile、采样和质量信息。

## 11. 清单校验与精确领取

批次审计会先读取并校验三个清单，再查询 SQLite。即使数据库里还没有本批任务，也可以先运行：

```bash
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json
```

此时 `complete=false` 只是表示尚未全部完成，不等于清单校验失败。校验至少拒绝：

- 总数不是 100，平台不是各 50，或任何一格不是 20/20/10；
- 重复父视频 ID，或子 manifest 的 `batch_id` 与总控不一致；
- category 与 content_kind 不匹配；
- B站非中文或 YouTube 非英文；
- `require_caption` 不是 false；
- `speaker_count=null` 却没有标成 `needs_review`，或分类与平台 profile 不匹配；
- playlist、直播、凭据 URL、签名媒体 URL；
- rights/AI 非法枚举或无依据的乐观标记；
- 绝对路径、`..`、未知顶层 schema version；
- manifest 大小、字符串长度或条目数超出上限。

静态校验不访问平台，也不能证明视频仍在线、时长未变化、语言正确或文件可下载。这些由
collect inspect、真实 canary 和最终 audit 继续验证。

采集入库后，下载器必须用精确批次过滤；`--source`、`--category` 只能进一步缩小范围，不能
替代 `--batch-id`：

```bash
python main.py --batch-id multispeaker-video-100-20260912 \
  --source bilibili --category 影视 --artifact-kind video_bundle \
  --limit 1 --workers 1

python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --category 影视 --artifact-kind video_bundle \
  --limit 1 --workers 1
```

领取过滤读取 SQLite `metadata_json` 中的
`source_data.bilibili.download_task.batch_id` 或 `source_data.youtube.job.batch_id`。没有该字段的
旧任务不会混入本批。B站的 `--limit` 限制的是任务行；一个父 BV 有多个分 P 时，不应把行数
误当成父视频数。

## 12. 运行时对账合同

`scripts/audit_multispeaker_batch.py` 输入总控 manifest，当前输出合同是：

| 维度 | 必需统计 |
|---|---|
| 顶层 | `batch_id`、`expected_parents`、`queued_parents`、`done_parents`、`complete` |
| 六格 | 每格 `expected_parents`、`queued_parents`、`done_parents`、`missing_parents`、`row_statuses` |
| 异常 | `missing`、`extras` |
| 基线 | 可用时校验旧库父 ID 数量、摘要以及与新清单是否重叠 |

候选阶段运行上面的无门禁命令；只有在要求 100 个父目标全部完成时，才增加严格门禁：

```bash
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json \
  --require-complete
python scripts/audit_media_queue.py
```

全库 `scripts/audit_media_queue.py` 仍必须运行，但它不能替代批次审计：前者证明所有 done
bundle 的统一闭包，后者证明这 100 个目标和六格配额恰好完成。
