# 统一采集与下载指南

## 先看实施状态

AudioSpider 的正式主路径只有一条：

```text
collect.py -> audiospider.db -> main.py -> downloads/<source>/<category>/
```

普通音频、B站完整分P和 YouTube 完整母视频都已使用这条路径。
`bilibili_dataset.py`、`youtube_dataset.py` 保留为兼容、修复和审计工具，不是新任务
主入口。

## 1. 准备环境

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
bash scripts/test.sh
```

确认没有冲突的正式 writer：

```bash
ps aux | grep -E 'collect.py|discover.py|main.py'
python main.py stats
df -h .
```

不要为了测试删除、替换或复制覆盖 `audiospider.db`。

## 2. 当前稳定的普通音频流程

先做临时 probe：

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 5 --timeout 60
```

确认来源可用后，正式采集元数据：

```bash
python collect.py --spiders podcast_rss
```

这一步写 `audiospider.db`，不下载媒体。查看队列：

```bash
python main.py stats
```

第一次只下载一条：

```bash
python main.py --source podcast_rss --limit 1 --workers 1 --format original
```

然后核对：

```bash
python main.py stats
find downloads/podcast_rss -type f | head
ffprobe -v error -show_streams -show_format /path/to/downloaded-audio
python -m json.tool /path/to/downloaded-audio.json
```

只有单条验证通过后才逐步增加 `--limit` 和 `--workers`。不要使用无限 limit、零循环
间隔或一开始运行所有来源。

## 3. B站完整分P

先检查 `config.py` 的 B站关键词、搜索页、视频和分P上限。正式采集会按该有界配置
搜索并把每个分P写成 `video_bundle`：

```bash
python collect.py --spiders bilibili
python main.py stats
```

第一次只下载一条：

```bash
python main.py --source bilibili --artifact-kind video_bundle --limit 1 --workers 1 --format original
```

实际流程：

1. `collect.py` 有界检查 BV/分 P/CID 和字幕目录；
2. 新 B站任务默认写为 `video_bundle`；数据库中的历史 audio 记录不被重解释；
3. `main.py` 刷新 DASH 表示层，分别下载视频与音频；
4. 合并完整 MP4并抽取 16 kHz mono PCM16 WAV；
5. 保存所有符合语言策略的公开字幕 JSON/VTT/TXT；
6. bundle audit 通过后提交数据库 `done`。

“尽量带字幕”表示字幕 best effort：匿名接口若返回
`need_login_subtitle=true`，sidecar 写 `auth_required`，媒体仍可完成。只有用户明确说
“必须有字幕”时，缺字幕才是硬失败。

人工、自动、unknown 必须逐轨判断。B站 `type` 是生成来源主证据，`ai_type` 是翻译
轴；不能因 `ai_type` 或 CDN 名称单独断言 ASR 来源。

## 4. YouTube 完整母视频

YouTube 来源读取受控 manifest。默认路径是 `config/youtube_sources.initial.json`；也可用
已实现的环境变量指定文件和本轮上限：

```bash
AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_sources.initial.json \
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 \
python collect.py --spiders youtube

python main.py --source youtube --artifact-kind video_bundle --limit 1 --workers 1 --format original
```

YouTube 核验和下载分别运行在可终止子进程中；默认硬超时为 120 秒和
14,400 秒，可通过 `AUDIOSPIDER_YOUTUBE_INSPECT_TIMEOUT` 与
`AUDIOSPIDER_YOUTUBE_DOWNLOAD_TIMEOUT` 调整。超时不会把未完整 bundle 提交为 `done`。

只有用户明确授权使用自己的 B站登录态时，才可在服务器私下设置
`BILIBILI_COOKIE` 并为本次 `main.py` 命令加 `--allow-bilibili-cookie`。
不加该开关时，即使环境中残留 Cookie 也会强制匿名。

采集阶段用 yt-dlp structured info 核验完整视频和同语言平台字幕，只写数据库；下载
阶段保存：

- 完整 MP4；
- 完整 16 kHz mono PCM16 WAV；
- 与内容语言同族的平台字幕；
- TXT 与 metadata sidecar。

默认不生成 clip。即使兼容 CLI 仍保留历史 clip 功能，`collect.py` 和 `main.py` 也不
会自动触发。用户显式要求 clip 时，应从已验收父 bundle 派生，保留父哈希和边界，
且不能覆盖母视频。

每条 manifest 可设置 `require_caption`：

- 省略或设为 `true`：保持历史严格行为，没有合格同语言字幕时拒绝完成；
- 设为 `false`：尽量获取同语言字幕，无字幕也保留完整母视频和 WAV。

有字幕时优先人工轨，自动轨必须保留 `platform_auto`。中文/粤语视频不使用
英文 fallback。平台完全没有字幕轨时记录 `caption.status=missing`；有其他语言轨但
没有同语言轨时记录 `no_matching_language`。这两种 bundle 不生成空 VTT/TXT。网络、
API 或解析异常仍然是失败，不能冒充无字幕。

## 5. 输出解释

普通音频：

```text
downloads/<source>/<category>/
├── <audio-id>.<ext>
├── <audio-id>.json
└── <audio-id>.<background-asset>
```

完整视频：

```text
downloads/<source>/<category>/<source-id>/<job-key>/
├── source.mp4
├── audio.wav
├── captions.<language>.<kind>.<id>.json  # B站原始字幕，若有
├── captions.<language>.<kind>.<id>.vtt
├── captions.<language>.<kind>.<id>.txt
└── metadata.json
```

YouTube best-effort 无字幕 bundle 的合法闭包精确为
`source.mp4 + audio.wav + metadata.json`。

`done` 的普通音频 `local_path` 指向音频文件；`done` 的视频任务用 `local_path` 指向
bundle 内 `source.mp4`，并用 `bundle_path` 指向 bundle 根目录。任何 `.part`、不完整
staging 或缺失 sidecar 都不是完成。

## 6. 重试与恢复

- `pending` 由 `main.py` 原子领取为 `downloading`；
- 活跃任务带 lease；
- 进程退出后只有 lease 到期任务可回到 `pending`；
- 普通音频和视频流都先写 `.part`；
- 只有精确匹配 `Content-Range` 才追加；HTTP 200 重新写，416 不算成功；
- failed 只做有界、按来源复核后的重试；
- 视频 bundle 必须在完整 audit 后才提交 done。

当前普通音频失败重试命令：

```bash
python main.py --retry-failed --source podcast_rss --limit 20 --workers 1
```

视频 bundle 也使用同一失败重试入口：

```bash
python main.py --retry-failed --source bilibili --limit 1 --workers 1 --format original
python main.py --retry-failed --source youtube --limit 1 --workers 1 --format original
```

`--format` 只影响普通音频，不改变视频 bundle 的 MP4+WAV 契约。

## 7. 迁移旧 standalone bundle

脚本默认是 dry-run，不移动文件、不登记数据库：

```bash
python scripts/migrate_video_bundles.py
python scripts/migrate_video_bundles.py \
  --legacy-root downloads/bilibili-video-20260910 \
  --legacy-root downloads/youtube-staging-20260909 \
  --report logs/video-bundle-migration-dry-run.json
```

检查 `failure_count=0`、来源/目标路径和数量后再 apply：

```bash
python scripts/migrate_video_bundles.py --apply \
  --report logs/video-bundle-migration-apply.json
```

`--apply` 会先用 SQLite backup API 在数据库旁生成带 UTC 时间戳的 backup，然后执行
幂等移动、再次验证 bundle，并把它登记成完成的 `video_bundle`。目标目录是：

```text
downloads/<source>/<category>/<source_id>/<job_key>/
```

脚本还支持 `--db /path/to/audiospider.db`。迁移时先停止正式 writer；不要用普通 `cp`
复制活动中的 WAL 数据库。

迁移后执行统一队列审计，同时核对 DB 行、`bundle_path`、文件闭包与孤儿目录：

```bash
python scripts/audit_media_queue.py
```

## 8. 凭据与网络安全

- 默认只访问公开、无凭据 HTTPS URL；
- 拒绝私网、loopback、link-local、凭据 URL和未重新校验的重定向；
- 不把 signed URL query 写入数据库、sidecar 或日志；
- 不从浏览器自动读取 Cookie；
- Bilibili Cookie 只在用户明确授权且有合法访问权时临时注入，只发往
  `api.bilibili.com`；
- Cookie 不得出现在聊天、Git、JSON、命令行参数或日志；
- yt-dlp、ffmpeg/ffprobe 子进程不继承爬虫凭据；
- 硬限制单流、字幕、bundle、任务数和磁盘保留空间。

## 9. 版权与数据质量

公开可访问不等于允许下载、训练、商业使用或再分发。所有媒体默认
`rights.status=needs_review`，只有可复核许可或授权证据才能升级。

还必须区分：

- 平台字幕 vs ASR；
- 人工平台轨 vs 自动平台轨；
- 字幕生成来源 vs 媒体 AI 生成来源；
- 内容语言 vs 字幕语言；
- 平台可见作者 vs 已人工验真；
- 有画面烧录字幕 vs 有独立字幕轨。

## 10. 统一实现验收清单

以下是正式实现 gate：

- collect 只增加数据库任务，不产生大媒体；
- B站新任务默认 `video_bundle`；
- YouTube 新任务只产生完整母视频；
- 普通音频目录与行为回归通过；
- 数据库按 source/artifact/status 对账；
- MP4 有视频+音频且满足画质/时长策略；
- WAV 为 16 kHz mono PCM16并与 MP4 对齐；
- 字幕语言与内容语言同族；
- subtitle JSON 能确定性重建 VTT/TXT；
- manual/automatic/unknown 与 AI 媒体状态独立；
- SHA-256、bytes、相对路径和 sidecar 闭包通过；
- 无 signed query、Cookie、绝对外逸路径；
- staging 为空，audit failure 为零。

设计与实施状态见[统一架构](architecture.md)和[实施计划](plans/2026-09-10-unified-media-pipeline.md)。
