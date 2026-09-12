# AudioSpider 统一媒体架构

> 实施状态：统一队列已实现。普通音频、B站完整分P和 YouTube 完整母视频都由
> SQLite 交接给 `main.py`；standalone 工具只保留兼容、修复和审计能力。

## 唯一正式主路径

```mermaid
flowchart LR
    EXT[公开来源]
    COL[collect.py\n只采集元数据]
    DB[(audiospider.db\n统一任务与状态)]
    MAIN[main.py\n领取、下载、验收]
    KIND{artifact kind}
    AUDIO[普通音频]
    VIDEO[完整视频 bundle]
    OUT[downloads/source/category]

    EXT --> COL --> DB --> MAIN --> KIND
    KIND -->|audio| AUDIO --> OUT
    KIND -->|video_bundle| VIDEO --> OUT
```

`discover.py` 可以发现播客 feed，但产生的正式节目任务仍写入同一个数据库。
正式下载只由 `main.py` 发起。

## 为什么编排统一、产物不统一

普通播客是一份音频文件；B站和 YouTube 需要一个目录闭包。统一的是：

- collect 与 download 分离；
- SQLite 任务、状态、lease、重试和统计；
- 安全网络访问、磁盘水位和字节上限；
- sidecar envelope、rights、AI 与字幕 provenance；
- SHA-256 和完成验收。

不能强行统一的是：

- B站的 BV、CID、DASH 与登录门禁；
- YouTube 的 video ID、yt-dlp 格式与人工/自动字幕字典；
- RSS 音频的 enclosure、章节、transcript 和公版原文；
- 文件闭包：音频是一个主文件，视频是多文件 bundle。

## 来源默认行为

| 来源 | 默认产物 | 说明 |
|---|---|---|
| RSS/小宇宙/喜马拉雅/LibriVox | `audio` | 当前行为保持不变 |
| B站 | `video_bundle` | 完整分P；历史 audio 记录保持兼容 |
| YouTube | `video_bundle` | 受控 manifest 中的完整母视频 |

B站新采集不再产生纯音频任务；数据库里的历史 B站 `audio` 记录仍可由普通音频
handler 完成。YouTube clip 不在默认主路径；只有用户明确要求时才可作为父 bundle
的派生产物。

## 跨平台批次分类与总控

视频批次使用三组受控分类，B站和 YouTube 共用同一语义：

| `content_kind` | `category/dataset_category` | 说明 |
|---|---|---|
| `screen_media` | `影视` | 影视、剧情、节目或平台发布的相关完整条目 |
| `interview_roundtable` | `访谈` | 访谈、对谈、圆桌 |
| `conference_forum` | `会议论坛` | 会议、论坛、峰会、panel |

100 条候选批次由一个总控文件引用两个平台 manifest。B站清单模式与搜索模式互斥；设置
`AUDIOSPIDER_BILIBILI_MANIFEST` 后，清单优先且不会在失败时回退搜索。YouTube 影视和
会议分别使用 `youtube_screen_parents`、`youtube_conference_forums`，两者都属于完整父
视频 profile，不会自动切片。

```mermaid
flowchart LR
    I[批次总控]
    B[B站精确 manifest]
    Y[YouTube 精确 manifest]
    C[collect.py]
    Q[(audiospider.db)]
    M[main.py<br/>--batch-id 精确领取]
    D[downloads/source/category]
    A[audit_multispeaker_batch.py]
    I --> B --> C
    I --> Y --> C
    C --> Q --> M --> D
    I --> A
    Q --> A
```

总控和清单存在只证明候选集合与 20/20/10 配额可被静态核验。当前候选的语言和多人线索
可为 `candidate_unverified`；在人工听审、真实下载和 bundle audit 前不能宣称批次完成。

## 状态机

```mermaid
stateDiagram-v2
    [*] --> pending: collect 入库
    pending --> downloading: SQLite 事务领取和 lease
    downloading --> done: artifact 完整验收后提交
    downloading --> failed: 传输、媒体或硬策略失败
    downloading --> pending: lease 超时回收
    failed --> pending: 显式有界重试
    done --> [*]
```

视频任务在 staging 中可能已经有部分流或字幕，但数据库只有在整个 bundle 通过
audit 后才进入 `done`。字幕 `auth_required` 不是队列状态，而是 sidecar 内的来源
事实。

## 目录结构

```text
downloads/
├── podcast_rss/<category>/<audio> + <sidecar>
├── xiaoyuzhou/<category>/<audio> + <sidecar>
├── ximalaya/<category>/<audio> + <sidecar>
├── librivox/<category>/<audio> + <sidecar>
├── bilibili/<category>/<source-id>/<job-key>/
│   ├── source.mp4
│   ├── audio.wav
│   ├── captions.*.json/.vtt/.txt
│   ├── visual_ocr.json/.vtt/.txt  # 显式 OCR 回退；无稳定 cue 时仅 JSON
│   ├── captions.*.rejected.json  # 错配时间轴隔离证据；没有派生 VTT/TXT
│   └── metadata.json
└── youtube/<category>/<source-id>/<job-key>/
    ├── source.mp4
    ├── audio.wav
    ├── captions.*.vtt/.txt  # 有同语言平台字幕时
    └── metadata.json
```

## 字幕与媒体 AI 是两条轴

| 字段 | 允许值 | 回答的问题 |
|---|---|---|
| `content_language` | BCP-47/项目规范，未知 `und` | 媒体主要说什么语言 |
| `caption.language` | 平台轨语言 | 字幕是什么语言 |
| `caption.kind` | `manual/automatic/unknown` | 平台字幕生成来源 |
| `caption.text_source` | `platform_manual/platform_auto/platform_unknown` | 文本来源证据 |
| `ai_generation.status` | `declared/not_declared/suspected/unknown` | 媒体本身是否 AI 生成 |
| `rights.status` | 默认 `needs_review` | 是否具备使用权证据 |

自动字幕不证明视频或音频由 AI 生成。平台 manual 也不等于逐字人工验真。字幕必须
与内容语言同族；中文/普通话/粤语内容不能用英文字幕兜底。

视觉 OCR 是独立的提取来源：

| 类别 | 证据来自 | `text_source` | 能说什么 |
|---|---|---|---|
| 平台人工轨 | 平台轨字段/标签 | `platform_manual` | 平台展示为普通 CC；不保证逐字人工验真 |
| 平台自动轨 | 平台轨字段/标签 | `platform_auto` | 平台展示为自动轨 |
| 画面字幕 OCR | MP4 可见像素 + 本地模型 | `visual_ocr` | 本地机器识别了烧录文字；原作者/制作方式未知 |

三者不能互相覆盖。`auth_required` 只说明匿名请求不能判定平台轨；随后即使 OCR 成功，也
必须保留该平台事实。OCR 的 `no_stable_text_detected` 也不能写成“视频没有字幕”。

```mermaid
flowchart TD
    Q[main.py 已完成 B站 bundle] --> P{存在合法同语言平台轨?}
    P -->|是| PT[保留 platform_manual/platform_auto]
    P -->|否；且显式启用| V[source.mp4 画面 OCR]
    P -->|否；未启用| S[保留平台 availability]
    V --> F[固定 ROI 按 fps 抽帧]
    F --> C[置信度过滤、跨帧匹配、静态覆盖层过滤]
    C --> G{有稳定 cue?}
    G -->|有| O[visual_ocr JSON/VTT/TXT]
    G -->|无| N[no_stable_text_detected；只保留 JSON]
    PT --> A[统一 bundle validator]
    S --> A
    O --> A
    N --> A
    A --> D[(同一 audiospider.db 与 downloads/bilibili/...)]
```

当前 v1 对配置的归一化区域（默认画面下半区）用 FFmpeg 固定约 4 fps 抽帧，时间戳来自
`fps` filter 输出帧序号，并保守记录一个采样间隔的不确定度；它不是逐帧原始 PTS。OCR 结果至少连续两个
采样、文本/位置相似才合并为 cue，覆盖视频过久的静态字会作为台标类覆盖层过滤。空间框会从
ROI 局部坐标映射回完整视频归一化坐标；时间边界保守记录 `1/fps` 不确定度。4 fps、
固定 ROI 和串行识别是可审计的初始 profile，不是所有视频的保证；快闪、移动、多区域或
双语字幕需调参/后续增强。PaddleOCR 是本轮唯一已接入的 L4 后端（仍须预装依赖和模型）；RapidOCR/ONNX、变化帧跳过和 GPU
批处理是后续可选优化，不能写成当前已具备。Whisper/音画对齐只能作为一致性证据，不能
替换像素 provenance。

YouTube 清单通过 `require_caption` 决定字幕是硬门禁还是 best effort。缺失字幕的完整
母视频只能在 `require_caption=false` 时完成，并必须以 `missing` 或
`no_matching_language` 保留原因；不创建假 VTT/TXT。外部请求异常始终进入失败路径。

## 当前正式命令

当前可运行：

```bash
python collect.py --spiders podcast_rss
python main.py --source podcast_rss --limit 1 --workers 1 --format original
python collect.py --spiders bilibili
python main.py --source bilibili --limit 1 --workers 1 --format original
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 python collect.py --spiders youtube
python main.py --source youtube --limit 1 --workers 1 --format original
python main.py stats
```

精确候选批次还可运行：

```bash
AUDIOSPIDER_BILIBILI_MANIFEST=config/bilibili_multispeaker_50_20260912.json \
python collect.py --spiders bilibili
AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_multispeaker_50_20260912.json \
python collect.py --spiders youtube
python main.py --batch-id multispeaker-video-100-20260912 \
  --source bilibili --artifact-kind video_bundle --category 影视 --limit 1 --workers 1
python main.py --batch-id multispeaker-video-100-20260912 \
  --source youtube --artifact-kind video_bundle --category 影视 --limit 1 --workers 1
python scripts/audit_multispeaker_batch.py \
  --batch-index config/multispeaker_video_100_20260912.batch.json
```

`--batch-id` 从两平台任务 metadata 精确过滤 pending 或 failed 行；`--source` 和
`--category` 只是六格切片，不能单独保证批次隔离。

`main.py --source` 同时过滤普通音频和视频 bundle。`--format` 只控制普通音频；视频
handler 始终生成完整 MP4 与 WAV。YouTube 默认读取 `config/youtube_sources.initial.json`，
可用 `AUDIOSPIDER_YOUTUBE_MANIFEST` 指定另一个受控 manifest。

旧 standalone bundle 使用迁移脚本。默认仅 dry-run：

```bash
python scripts/migrate_video_bundles.py
python scripts/migrate_video_bundles.py --legacy-root /path/to/legacy-root
```

确认报告无失败后才执行：

```bash
python scripts/migrate_video_bundles.py --apply
```

`--apply` 先用 SQLite backup API 备份数据库，再把已验收 bundle 移到
`downloads/<source>/<category>/<source_id>/<job_key>/` 并登记为 done。可用 `--db` 和
`--report` 指定数据库及 JSON 报告。

迁移步骤与测试门见[实施计划](plans/2026-09-10-unified-media-pipeline.md)。完整设计与
失败模式见[设计文档](plans/2026-09-10-unified-media-pipeline-design.md)，决策理由见
[ADR-002](design/adr-002-unified-media-artifacts.md)。

## 验收

当前文档和代码基线：

```bash
bash scripts/test.sh
python scripts/check_docs.py
python doctor.py
python main.py stats
git diff --check
```

统一实现的真实验收必须顺序执行一条普通音频、一个 B站分 P 和一个 YouTube
母视频，并核对数据库状态、ffprobe、字幕语言/provenance、SHA-256、sidecar、磁盘
字节与 staging 清空。不得用合成 fixture 宣称真实平台字幕覆盖。
