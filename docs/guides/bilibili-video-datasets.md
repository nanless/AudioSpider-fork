# B站完整视频、WAV 与同语言字幕指南

本指南面向第一次操作 AudioSpider 的用户。当前 B站视频的**唯一正式路径**是：

```text
collect.py -> audiospider.db -> main.py -> downloads/bilibili/<category>/<source_id>/<job_key>/
```

这里统一的是任务编排、SQLite 状态、断点续跑和验收；每条 B站任务的产物仍是一个
完整 bundle，而不是单个音频文件。历史 `artifact_kind=audio` 的 B站记录继续保留原语义，
但新采集默认是 `video_bundle`。

`bilibili_dataset.py` 只用于兼容旧数据、修复或单 bundle 审计，不是新批次的正式入口。
不要再为正式任务创建 `downloads/bilibili-video-日期/` 之类的独立数据根目录。

## 1. 一个正式样本包含什么

```text
downloads/bilibili/访谈/BVxxxx_p1/<job_key>/
├── source.mp4
├── audio.wav
├── captions.zh-Hans.manual.<track-id>.<index>.json  # 有合法轨道时才存在
├── captions.zh-Hans.manual.<track-id>.<index>.vtt
├── captions.zh-Hans.manual.<track-id>.<index>.txt
├── captions.zh-Hans.manual.<track-id>.<index>.rejected.json  # 错配轨道才存在
└── metadata.json
```

- `source.mp4`：该分 P 的完整视频和音频，不截 clip。
- `audio.wav`：由完整视频抽取的 16 kHz、单声道、PCM16 WAV。
- `captions.*.json`：B站返回的字幕结构；保留 cue 内容与平台字段，但会移除签名 URL 查询参数和传输凭据。
- `captions.*.vtt/.txt`：确定性派生的时间轴字幕和纯文本。
- `metadata.json`：BV、CID、分 P、作者、统计、字幕 provenance、rights、AI 状态、
  文件大小及 SHA-256 闭包。

没有同语言字幕时，`require_caption=false` 的样本仍可包含 MP4、WAV 和
`metadata.json` 并完成；程序不会用本地 ASR 伪造平台字幕。

显式启用视觉 OCR 回退后，同一目录还可能有 `visual_ocr.json/.vtt/.txt`。它们来自
`source.mp4` 像素，不是 B站字幕 API；无稳定 cue 时只保存 JSON。

## 2. 先做运行前检查

```bash
ssh dev_L4_1gpus
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider

python doctor.py
python main.py stats
ps aux | grep -E 'collect.py|discover.py|main.py'
df -h .
```

确认没有另一个正式 writer 正在迁移数据库或竞争同一批任务。不要删除
`audiospider.db`、`audiospider.db-wal` 或 `audiospider.db-shm`。

## 3. 用环境变量限定本轮搜索

B站来源不是读取 standalone manifest，而是由 `collect.py` 使用受控搜索参数发现任务。
常用变量如下：

| 变量 | 含义 |
|---|---|
| `AUDIOSPIDER_BILIBILI_CONTENT_LANGUAGE` | 内容主要语言；中文批次设为 `zh` |
| `AUDIOSPIDER_BILIBILI_CATEGORY` | 可选统一分类覆盖；访谈批次设为 `访谈` |
| `AUDIOSPIDER_BILIBILI_KEYWORDS` | 逗号分隔的搜索词 |
| `AUDIOSPIDER_BILIBILI_REQUIRED_TITLE_TERMS` | 标题至少命中其中一个词；空值表示不启用 |
| `AUDIOSPIDER_BILIBILI_EXCLUDED_TITLE_TERMS` | 标题命中其中一个词就排除；只用于候选筛选，不是声学语言证明 |
| `AUDIOSPIDER_BILIBILI_MAX_SEARCH_PAGES` | 每个关键词最多搜索页数 |
| `AUDIOSPIDER_BILIBILI_MAX_VIDEOS_PER_KEYWORD` | 每个关键词最多解析多少个 BV |
| `AUDIOSPIDER_BILIBILI_MAX_PAGES_PER_VIDEO` | 每个 BV 最多采集多少个分 P；完整单视频批次通常设为 `1` |
| `AUDIOSPIDER_BILIBILI_MAX_NEW_RECORDS` | 本轮最多**真实新增**多少个父 BV；一个父 BV 的有效分 P 会完整入库；`0` 表示不限 |
| `AUDIOSPIDER_BILIBILI_MIN_DURATION_SECONDS` | 单分 P 最短时长 |
| `AUDIOSPIDER_BILIBILI_MAX_DURATION_SECONDS` | 单分 P 最长时长 |
| `AUDIOSPIDER_BILIBILI_JOB_ATTEMPTS` | 单任务遇到临时 API/CDN 故障时最多尝试次数；默认 `3` |
| `AUDIOSPIDER_BILIBILI_RETRY_BACKOFF_SECONDS` | 重试线性退避基数秒数；默认 `10` |
| `AUDIOSPIDER_BILIBILI_PROXY` | 可选的 B站专用 loopback HTTP 代理；格式只能是 `http://127.0.0.1:端口` |

`MAX_NEW_RECORDS` 按 SQLite 去重后的真实新增父 BV 数计数；数据库中已有任一分 P 的父
BV 不会重复进入本批，也不会消耗新增配额。所有上限只控制 `collect.py`；采集阶段只写
元数据，不下载大媒体。

先用很小的范围做冒烟：

```bash
AUDIOSPIDER_BILIBILI_CONTENT_LANGUAGE=zh \
AUDIOSPIDER_BILIBILI_CATEGORY=访谈 \
AUDIOSPIDER_BILIBILI_KEYWORDS='人物访谈 长视频' \
AUDIOSPIDER_BILIBILI_REQUIRED_TITLE_TERMS='访谈,专访,对谈,对话,圆桌,会谈,播客' \
AUDIOSPIDER_BILIBILI_EXCLUDED_TITLE_TERMS='俄语,英语,英文,日语,韩语,法语,德语,西班牙语' \
AUDIOSPIDER_BILIBILI_MAX_SEARCH_PAGES=1 \
AUDIOSPIDER_BILIBILI_MAX_VIDEOS_PER_KEYWORD=10 \
AUDIOSPIDER_BILIBILI_MAX_PAGES_PER_VIDEO=1 \
AUDIOSPIDER_BILIBILI_MAX_NEW_RECORDS=3 \
AUDIOSPIDER_BILIBILI_MIN_DURATION_SECONDS=1500 \
AUDIOSPIDER_BILIBILI_MAX_DURATION_SECONDS=14400 \
python collect.py --spiders bilibili
```

标题包含/排除规则只是选择候选的证据，不能证明实际音轨一定为中文。正式数据仍应抽听
或用独立语言识别质检，并把语言核验来源单独记录。

如果服务器直连 B站暂时不可用，而你已经建立了受控的本机反向代理隧道，可在采集和
下载命令前临时加：

```bash
AUDIOSPIDER_BILIBILI_PROXY=http://127.0.0.1:18443 \
python collect.py --spiders bilibili

AUDIOSPIDER_BILIBILI_PROXY=http://127.0.0.1:18443 \
python main.py --source bilibili --artifact-kind video_bundle \
  --category 访谈 --language zh --limit 1 --workers 1 --format original
```

该入口只显式传给 B站请求，不读取 `HTTP_PROXY`/`HTTPS_PROXY`，不影响同进程的其他来源。
代理值不会进入 SQLite、sidecar 或日志。服务端只接受 `127.0.0.1` 的无凭据纯 HTTP
入口；本机代理还应独立限制可访问的 B站 API、字幕和媒体 CDN 域名。不要用它绕过
付费、DRM、验证码、地区或账户访问控制。

采集后查看，不要直接假设发现数等于新增数：

```bash
python main.py stats
sqlite3 -readonly audiospider.db \
  "SELECT status,COUNT(*) FROM audio_urls WHERE source='bilibili' AND artifact_kind='video_bundle' GROUP BY status;"
```

## 4. 正式下载仍由 `main.py` 完成

第一次只取 1 条：

```bash
python main.py --source bilibili --artifact-kind video_bundle \
  --category 访谈 --language zh --limit 1 --workers 1 --format original
```

确认 MP4、WAV 和 sidecar 都正常后，再把 `--limit` 和 `--workers` 逐步调高。`--format`
只影响普通音频；B站 `video_bundle` 始终产出完整 MP4 与 WAV。

下载时会重新获取当前 DASH 表示层和短期签名 URL。签名 URL 不会写入数据库、sidecar
或恢复状态。视频流和音频流先进入确定性 staging，合并并通过 bundle 验收后才整体提交
到 `downloads/bilibili/...`，随后数据库才进入 `done`。

## 5. 登录态与 Cookie：只在内存中使用

公开接口经常只能返回 `need_login_subtitle=true`。默认程序不读取浏览器 Cookie；即使
服务器环境中恰好存在 `BILIBILI_COOKIE`，没有 `--allow-bilibili-cookie` 也会强制匿名。

只有用户明确授权且对内容有合法访问权时，才可把本机已登录会话一次性传给当前
`main.py` 进程。推荐由受控脚本通过 SSH 标准输入注入，Cookie 只成为该进程的临时
环境变量，进程结束后立即清除。人工操作示例：

```bash
read -r -s BILIBILI_COOKIE
export BILIBILI_COOKIE
python main.py --source bilibili --artifact-kind video_bundle \
  --category 访谈 --language zh --limit 1 --workers 1 \
  --format original --allow-bilibili-cookie
unset BILIBILI_COOKIE
```

禁止把 Cookie 放进：

- 命令行参数、聊天消息或 shell 脚本字面量；
- `.env`、JSON manifest、SQLite、日志或 sidecar；
- Git 提交、下载目录或故障报告。

登录 Cookie 只允许发往 `api.bilibili.com`，不能转发给媒体 CDN、字幕 CDN、ffmpeg 或
其他子进程。登录成功也不保证视频一定提供字幕。

统一下载器会在启动时一次性消费 `BILIBILI_COOKIE` 并从环境删除；随后一批任务只共享
内存中的已校验值。下载 HTTP 会话使用 `DummyCookieJar`，不接受平台响应的持久 Cookie，
也不再先访问首页“预热”。这避免匿名响应字段混入或覆盖 Edge 登录态。

## 6. 字幕状态与人工/自动分类

每条字幕轨必须独立记录：

| `kind` | `text_source` | 能得出的结论 |
|---|---|---|
| `manual` | `platform_manual` | 平台以普通 CC 轨展示；不等于逐字人工验真 |
| `automatic` | `platform_auto` | 平台字段/标签表明是自动字幕通道 |
| `unknown` | `platform_unknown` | 证据缺失、未来枚举或字段冲突 |

B站 `type` 是字幕生成来源的主判定信号；`ai_type` 是独立的翻译轴，不能单独用它断言
ASR。自动字幕也不能证明视频声音或画面本身由 AI 生成。

没有可保存轨道时要区分：

| `caption.status` | 含义 |
|---|---|
| `auth_required` | 平台明确提示需要登录，匿名状态不能判断“真的没有” |
| `not_provided_publicly` | 公开接口成功，且明确没有轨道 |
| `no_matching_language` | 有字幕，但没有与 `content_language` 同族的轨道 |
| `invalid_track_inventory` | 平台曾返回轨道，但 URL/字段不符合安全规则；不等于没有字幕 |
| `invalid_timeline` | 同语言轨道全部错配当前分 P；保留视频、数值证据和隔离的净化平台 JSON，不生成 VTT/TXT |
| `unknown` | 证据不足 |
| `downloaded` | 至少一条同语言轨已保存并进入文件闭包 |

中文内容只接受中文/普通话/粤语同族字幕，不拿英文字幕兜底。画面上的烧录字幕只是
像素，不等于平台提供了可下载字幕轨。

视觉 OCR 是第三类来源：`text_source=visual_ocr`、`extraction_kind=automatic`，原作者写
`caption_authorship=unknown`。它不改变原来的 `caption.status`：平台仍为 `auth_required`
时，即使 OCR 成功，也必须保留“平台轨无法判定”的事实。

当前 v1 用 FFmpeg 对配置 ROI（默认下半画面）固定约 4 fps 抽帧；以 fps 输出帧序号恢复
时间并保守记录一个采样间隔的不确定度。低置信度结果被过滤，至少两个连续采样的相似文本/位置才组成
cue，覆盖过久的静态文字会按台标类覆盖层过滤。它尚不是自适应 ROI、变化帧跳过或 GPU
批处理：快闪、移动、多区域、弹幕、人名条和中英双语都需要调参和人工抽检。

### 6.1 为什么 sidecar 会有 `inventory_attempts`

B 站 player 字幕列表可能在短时间内波动。每次查询只保存原始轨道数、已选轨道数、
语言、人工/自动类型字段、URL 值类型、scheme 形式、host 以及拒绝原因。不会保存完整
URL、path、query、fragment、Cookie 或代理地址。

只有第一次是 `provided`，但没有任何可选轨道时，程序才短暂等待并刷新 **1 次**。
第一次是 `invalid_track_inventory`、第二次又变成空列表时，最终仍保留该状态，
不会把曾观测到的异常证据降级成“公开无字幕”。
如果第二次查询本身抛出网络/API 异常，`require_caption=false` 会回退到第一次证据，
并只记录 `inventory_status=refresh_failed` 与异常类名 `error_type`，不记录异常消息。
`require_caption=true` 属于 strict 路径，该刷新失败会直接使任务失败。

平台偶尔会把整段或其他分 P 的字幕返回给极短分 P。程序使用
ffprobe 实际视频时长与最后 cue 对比；超出 2 秒以上就标为
`invalid_timeline`。这时 best-effort 任务仍保留完整 MP4/WAV，并把净化后的平台 JSON
放在 `.rejected.json` 隔离文件中；cue 内容不截断、不伪造，传输凭据会移除，并且不
生成误导性 VTT/TXT。若一条错配而另一条合法，合法轨仍保留，整体标为
`downloaded` + `payload_status=partial`。strict 任务至少有一条合法轨才成功。

## 7. 断点续跑与失败重试

- SQLite 负责 `pending -> downloading -> done/failed` 状态和 lease。
- staging 中的 `.part` 只有在表示层指纹一致、服务端返回精确 `Content-Range` 时才追加。
- HTTP 200 会重新写入；HTTP 416 不会被误判为已完成。
- 同一 `job_key` 的已完成 bundle 会复用，不重复下载。
- Cookie 不持久化，因此需要登录态的重试必须再次进行内存注入。
- API 超时、HTTP 429/5xx、`code=-412` 和所有 DASH CDN 候选暂时失败会在同一任务内
  有限退避重试；每轮刷新平台 API/签名 URL，并复用安全 `.part`。
- CID 变化、语言/时长/路径策略错误不会重试，避免把永久错误变成无界循环。

先分类失败，再做小批重试。重试会继承 source/category/language/time/artifact 过滤：

```bash
python main.py --retry-failed --source bilibili --category 访谈 --language zh \
  --artifact-kind video_bundle --limit 5 --workers 1 \
  --format original --allow-bilibili-cookie
```

不要无边界地反复重试下架、地区限制、权利受限或长期不可访问的视频。

### 7.1 只回填字幕，不重下 MP4/WAV

已完成 bundle 的字幕状态可用下列工具重新查询。第一次必须省略 `--apply`：

```bash
python scripts/backfill_bilibili_captions.py --limit 20
```

预览仅列出 SQLite 中状态为 `done` 、且路径/身份/文件闭包都匹配的 B 站任务；不请求
平台、不备份、不写 sidecar 或 SQLite。人工确认后才执行：

```bash
AUDIOSPIDER_BILIBILI_PROXY=http://127.0.0.1:18443 \
python scripts/backfill_bilibili_captions.py --limit 20 --apply \
  --allow-bilibili-cookie
```

若同语言平台轨不可用，并且确实要识别画面烧录字幕，先只预览一条：

```bash
python scripts/backfill_bilibili_captions.py --help
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr --apply
```

本轮 Paddle 后端可通过 `--ocr-engine paddle` 选择，采样、区域、最低置信度和 profile 可用
对应 `--ocr-*` 参数调整；以部署版本 `--help` 为准。OCR 读取本地 MP4，不需要
`--allow-bilibili-cookie`；该门禁只用于经明确授权的平台 API 重查。两种回填共享 SQLite
backup、精确 job lock、rollback 和 CAS，不改写 `source.mp4`/`audio.wav`。

apply 后运行统一审计并抽看原画面。`downloaded` 只表示有 cue 通过置信度/连续帧技术门，
文档仍是 `human_review_status=unreviewed`。批次级验收应在标注集报告 cue precision/recall、
中文 CER、边界误差 p95、误报/分钟、RTF、GPU 峰值和复核率。可先以 precision ≥99%、
recall ≥95%、CER ≤5%、边界 p95 ≤250 ms、误报 ≤0.02 cue/分钟、RTF ≤0.3 作为项目目标；
它们不是 PaddleOCR 或当前实现的性能承诺，未实测前不能宣称达标。

可捕获异常会恢复文件和 sidecar/SQLite。`SIGKILL`/断电后应保留 backup 与 rollback 目录，
先运行审计，再按报告和闭包哈希恢复或重跑精确 job；不要盲删或手工改 DB。

`invalid_timeline` 默认不再被每次回填重复选中，避免稳定错配的低 ID 样本
占满 `--limit` 并饿死后续任务。只有确认平台轨道可能已更新时，才加
`--retry-invalid-timeline`。

`--apply` 会先用 SQLite backup API 在数据库旁创建
`audiospider.db.caption-backfill-<UTC>.bak`，然后按精确 `job_key` 获取排他锁。每条任务只可
新增合法字幕 JSON/VTT/TXT 或错配轨道 `.rejected.json`、原子替换 `metadata.json`、
并同步 SQLite 闭包大小与哈希；
不会下载或改写 `source.mp4`/`audio.wav`。任一验收或数据库步骤失败，当前任务的
sidecar、新字幕文件和 SQLite 事务会回滚。

兼容旧数据时，`requested_languages: []` 不会被理解为“任意语言都可以”；
工具会根据 `content_language` 恢复中文或其他同语言候选。单条失败时，
报告会给出 `phase` （例如 `inventory`/`caption_payload`/`commit`）和固定
`reason` 码，不记录平台签名地址、Cookie 或原始异常文本。

回填会保存发现时 SQLite 的旧 `file_size/content_hash`，并在修改前重新计算磁盘闭包；两者
不一致就拒绝操作。ffprobe/多 GB 哈希、字幕验收都在 job lock 内但在 SQLite 写事务外完成。
最后只开一个短 `BEGIN IMMEDIATE`，复核完整旧行并用旧闭包值作为 CAS 条件更新。

程序可捕获正常异常以及 `KeyboardInterrupt`/`SystemExit` 等 `BaseException`，并幂等恢复当前 bundle。
但文件系统和 SQLite 是两个提交域，该工具**无法保证** `SIGKILL`、断电、内核崩溃时的跨文件系统/
数据库原子性。中断后应先保留 backup，运行 `scripts/audit_media_queue.py`，再根据 sidecar 闭包与
SQLite 指纹决定恢复数据库或重跑单个 job，不要盲目删文件。

## 8. 审计正式产物

```bash
python scripts/audit_media_queue.py
```

统一审计会核对：数据库 done 行、`bundle_path`、`source_id/job_key` 目录身份、MP4/WAV、
字幕闭包、文件字节数、SHA-256、`local_path=.../source.mp4` 以及孤儿目录。

OCR bundle 还要核对 `derived_text.visual_ocr`、输入视频哈希、引擎/模型/profile、ROI、采样
时基与不确定度、cue 置信度/observations、质量计数，以及 JSON 能否确定性重建 VTT/TXT。
Whisper/音画一致性只能是额外复核证据，不能用 ASR 强行改写 OCR cue/provenance。

单条样本还应抽查：

```bash
ffprobe -v error -show_streams -show_format \
  downloads/bilibili/访谈/<source_id>/<job_key>/source.mp4
ffprobe -v error -show_streams -show_format \
  downloads/bilibili/访谈/<source_id>/<job_key>/audio.wav
python -m json.tool \
  downloads/bilibili/访谈/<source_id>/<job_key>/metadata.json
```

`failure_count=0` 和 `orphan_count=0` 只表示技术闭包通过，不表示已获得训练、再分发或
商业使用权。`rights.status` 默认保持 `needs_review`。

## 9. standalone CLI 什么时候还能用

`bilibili_dataset.py` 仅用于：

- 检查或修复迁移前的旧 standalone bundle；
- 对单个旧 bundle 执行兼容验证；
- 为迁移脚本提供底层解析/验证能力；
- 用户明确要求的隔离兼容实验。

旧目录应通过默认 dry-run 的 `scripts/migrate_video_bundles.py` 迁入统一树；不要把新正式
批次继续下载到旧根目录。迁移后再次运行 `scripts/audit_media_queue.py`。

本次 100+50 访谈批次的完整步骤见
[150 个中英文完整访谈批次运行手册](interview-batches-150.md)。
