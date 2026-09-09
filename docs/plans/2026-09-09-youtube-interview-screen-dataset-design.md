# YouTube 长访谈与影视短片数据集设计

## 目标

在不改变现有 `audio_urls` 音频下载队列语义的前提下，新增一条可复现、可审计的 YouTube 数据集流水线：

1. `youtube_interviews`：下载 25.5–60.6 分钟的多人长访谈/播客，保留原视频、独立音频、平台字幕和完整 sidecar。
2. `youtube_screen_clips`：下载影视/节目来源视频及字幕，再按字幕时间轴生成 0.418–29.888 秒的短视频、短音频、字幕片段和完整 sidecar，目标平均时长约 11.5 秒。

两类数据都只处理无需登录即可访问的公开视频。不绕过 DRM、付费墙、验证码、地区限制或平台访问控制。

## 为什么使用独立流水线

现有 `audio_urls` 表围绕“一个远程音频 URL 对应一个本地音频文件”设计。YouTube 数据同时包含父视频、字幕轨、派生音频、多个短片段和层级化元数据，强行写入原表会造成以下问题：

- 一个视频产生多个文件，无法用单个 `local_path` 完整表达；
- YouTube 媒体链接带短期签名，不适合作为持久唯一键；
- 短片段是本地派生物，不应伪装成远程音频 URL；
- 视频下载失败不应影响已有 RSS、Bilibili 等正式队列。

因此新增 `youtube_dataset.py`，使用 JSON/JSONL 清单和目录级 sidecar 管理视频数据；已有 `collect.py`、`main.py` 和正式 SQLite 数据库保持不变。

## 输入和运行阶段

输入清单是 JSON 文档，包含一个或多个公开视频 URL。每项必须声明采集 profile；可选声明人工核验的说话人数、语言、节目/影视来源、版权备注和授权依据。

流水线分四个阶段：

1. `inspect`：使用 yt-dlp 的结构化元数据接口读取视频信息和字幕轨，不下载媒体；校验 URL、时长、字幕可用性和 profile。
2. `download`：下载最高 720p 的视频和音频，优先人工字幕，允许自动字幕回退。
3. `clip`：仅对 `youtube_screen_clips`，解析 WebVTT 字幕并合并相邻字幕提示，生成满足时长范围的短片。
4. `audit`：验证媒体可解码、字幕时间有效、派生文件齐全、摘要哈希一致且 sidecar 未保存带签名查询参数的临时 URL。

## 字幕策略

字幕选择顺序为：

1. 请求语言的人工平台字幕；
2. 请求语言的自动平台字幕；
3. 同一基础语言的人工字幕；
4. 同一基础语言的自动字幕；
5. 未明确请求语言时，优先任意人工字幕，再选自动字幕。

每个 sidecar 必须包含：

- `caption_kind`: `manual` 或 `automatic`；
- `text_source`: `platform_manual` 或 `platform_auto`；
- `caption_language`、`caption_name`、`caption_ext`；
- `caption_status`: `available`、`downloaded`、`missing` 或 `failed`。

自动字幕只作为文本参考，绝不标成项目 ASR 或人工转写。当前两个 profile 都以平台字幕为必需资产；若没有字幕，直接拒绝该视频，不生成不完整 bundle。

## 说话人数与语言

说话人数不能由标题或字幕条数推断：

- 清单明确提供并经人工核验时：`speaker_count_status=verified_manual`；
- 未提供时：`speaker_count=null`、`speaker_count_status=needs_review`；
- 超出 profile 目标范围时默认拒绝，除非显式覆盖并保留原因。

语言同时保存清单声明值和平台字幕语言。影视短片不会把父视频的说话人数直接当作每个片段的精确人数；片段默认仍为 `needs_review`。

## 目录结构

```text
downloads/youtube/
├── interviews/<video_id>/
│   ├── source.mp4
│   ├── audio.wav
│   ├── captions.<lang>.manual|automatic.vtt
│   ├── captions.<lang>.manual|automatic.txt
│   └── metadata.json
├── screen_sources/<video_id>/
│   └── 与访谈父视频相同的源文件集合
├── screen_clips/<language>/<video_id>/<clip_id>/
│   ├── clip.mp4
│   ├── audio.wav
│   ├── captions.vtt
│   ├── transcript.txt
│   └── metadata.json
└── manifests/
    ├── inspected.jsonl
    ├── downloaded.jsonl
    ├── clips.jsonl
    └── audit.json
```

## 短片生成规则

- 从 WebVTT 有效 cue 开始，清除标签和重复行；
- 合并连续或轻微重叠的 cue，优先靠近 11.5 秒；
- 严格保证 `0.418 <= duration <= 29.888`；
- 不跨越超过 5 秒的明显长空白，也不生成负时间、零时长或越过父视频结尾的切片；平台原始字幕尾部轻微越界时保留原件但裁剪派生时间；
- 使用 ffmpeg 同时生成 MP4 和 16 kHz 单声道 PCM WAV；
- 字幕片段重写为从 0 开始的相对时间，并保存纯文本；
- 每个派生文件计算 SHA-256。

目标平均 11.5 秒是批次统计目标，不是每个片段都固定 11.5 秒。`audit` 会报告实际平均值和分位数，不会为了凑均值截断正在说话的字幕 cue。

## Sidecar 最小字段

父视频和短片都保存：schema 版本、profile、YouTube 视频/频道 ID、标题、公开页面 URL、发布时间、抓取时间、时长、语言、字幕来源、说话人数及其核验状态、AI 生成声明/证据、许可/权利备注、yt-dlp 版本、媒体流信息和 SHA-256。

短片额外保存父视频 ID、起止时间、原始字幕 cue 数量和相对字幕文本。所有远程媒体/字幕临时 URL 在持久化前移除查询参数，避免泄露签名或制造不可复现信息。

## 失败恢复和幂等性

- 目录以稳定 `video_id` 和确定性 `clip_id` 命名；
- 已存在且哈希/ffprobe 通过的文件复用；
- 下载先写临时文件，成功后原子替换；
- 单视频失败记录到 JSONL，不影响其他视频；
- 重跑不会重复生成相同片段；
- `audit` 只读，可在任何时候独立运行。

## 验收标准

- 单元测试覆盖 profile 校验、字幕选择、VTT 解析/合并、路径安全、URL 脱敏、sidecar 和命令构造；
- 离线集成测试使用合成视频，验证 MP4/WAV/VTT/TXT/JSON 成套生成；
- 全仓 `scripts/test.sh` 通过；
- 至少实际下载一个长访谈和一个带平台字幕的视频源，并生成一批影视短片；
- 所有实际样本通过 ffprobe、字幕时间和 sidecar 审计；
- 自动字幕样本可由机器字段和中文报告直接识别。
