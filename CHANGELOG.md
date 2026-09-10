# 变更记录

本文件记录面向使用者的重要变化。开发中的细节以 Git 历史为准。

## Unreleased

- 修复长视频批次超过两小时后 lease 过期、被其他来源 downloader 误回收的问题：活跃批次现在周期性续租，过期回收也限制在当前领取过滤范围内。
- B 站同语言字幕若与当前分 P 时长明显错配，best-effort 任务现在保留完整视频和隔离的净化平台 JSON（保留 cue 与平台结构、移除传输凭据），逐轨记录版本化时间轴证据，不生成误导性 VTT/TXT；所有轨道都错配时标记 `invalid_timeline`，严格字幕任务仍失败。

### 统一媒体队列

- 正式主路径统一为 `collect.py -> audiospider.db -> main.py -> downloads/<source>/<category>/`；
  普通音频、B站完整分P和 YouTube 完整母视频共享 SQLite claim、lease、重试和统计。
- `audio_urls` 加法式新增 `artifact_kind` 与 `bundle_path`；旧记录保持 `audio`，视频使用
  `video_bundle`，统计按 artifact kind 分组。
- `collect.py --spiders bilibili` 现在默认写入完整分P视频任务；历史 B站 audio 记录仍可
  由普通音频 handler 完成。
- 新增 `YoutubeSpider`；`collect.py --spiders youtube` 从受控 manifest 核验完整母视频和
  同语言字幕后入队，不默认生成 clip。
- `Downloader` 在统一领取后通过 `media_artifacts.py` 分派 B站/YouTube bundle，输出到
  `downloads/<source>/<category>/<source_id>/<job_key>/`；`local_path` 指向 `source.mp4`，
  `bundle_path` 指向完整目录。
- 新增 `scripts/migrate_video_bundles.py`：默认 dry-run；显式 `--apply` 时先用 SQLite
  backup API 备份数据库，再幂等移动、复验和登记旧 standalone bundle。
- 新增统一架构、下载指南、sidecar schema、ADR-002、设计/实施记录和三份仓库 Skill
  草案；standalone dataset CLI 调整为兼容、修复和审计定位。

### 纠偏

- B 站完整视频从误建的 `datasets/` 迁移到 `downloads/bilibili-video-20260910/`；以后视频数据统一放在 `downloads/`。
- YouTube 正式流程只保存完整父视频，不再默认生成 clip；CLI 生成 clip 必须显式提供 `--allow-clips`。
- YouTube/B站字幕强制与内容语言一致：英文配英文，中文/粤语配中文或粤语，其他语言按主语言代码一致。
- 历史 YouTube clips 和两条跨语言字幕父视频移入可恢复 `archive/`，没有删除。

### 新增

- 面向新手的中文根 README 和分层文档中心。
- `doctor.py`：只读环境、磁盘、ffmpeg/libopus 和 SQLite 诊断。
- `probe.py`：五种来源的有界真实爬取探针，默认使用临时数据库。
- LibriVox 每本书章节数上限，防止单本大书突破探针或正式采集预期。
- 正式库探针按当前来源判定结果和输出样例，同时分开报告全库与来源记录数。
- Conda 初始化、统一测试、探针包装和文档链接检查脚本。
- `Makefile` 常用命令入口和 `.env.example`。
- 工具脚本单元测试。
- 音频背景信息：可查询公共列、版本化来源 JSON、description、封面、公开 transcript、章节和公版原文。
- `main.py background` 为既有物理音频补齐背景文件，无需重复下载音频。
- RSS/Podcasting 2.0、小宇宙、喜马拉雅、LibriVox 和 B站的来源级元数据提取。
- 在大规模采集前，按稳定来源 ID 审计并定向回填全部历史详细信息。
- `youtube_dataset.py`：提供 YouTube 完整母视频、16 kHz WAV、平台人工/自动字幕和
  sidecar 的底层兼容、修复与审计能力，并由统一 dispatcher 复用。
- 字幕对齐影视短片：生成可追溯的 MP4/WAV/VTT/TXT/JSON 五件套，严格限制 0.418–29.888 秒。
- YouTube 清单探测、稳定 job/clip ID、目录级 staging、SHA-256/ffprobe/VTT 全量审计。
- `bilibili_dataset.py`：提供 B站分P DASH、MP4/WAV、平台字幕和 bundle audit 的底层
  兼容能力，并由统一 dispatcher 复用。
- `bilibili_subtitles.py`：人工、自动、未知三态来源判定，独立翻译维度，以及平台 JSON 到 VTT/TXT 的严格转换。
- B 站视频 bundle 使用表示层指纹断点续传、目录锁、原子提升、SHA-256/ffprobe/字幕重建审计。

### 安全

- 背景资产使用公网目标与重定向校验、MIME 白名单、单资产/总量/数量上限。
- sidecar 递归移除 URL 查询参数，避免持久化临时签名。
- 背景资产失败不会影响已验证音频的 done 状态。
- YouTube 仅接受无凭据单视频 URL，不用 Cookie、不绕过访问控制、不保存临时签名 URL；未核权样本明确标成候选。
- B 站媒体和字幕只接受受控 HTTPS 域名，不保存签名 query；可选登录 Cookie 只从进程环境读取且不持久化。
- 批量 Opus 转码会跳过带 `.audiospider-dataset.json` 保护标记的视频数据集。

### 文档

- 新手概念、安装和首次运行。
- 发现采集、下载转码、运维和故障排查。
- CLI、配置、数据库与来源适配器参考。
- 架构、安全、数据流和开发说明。
- 背景信息深度研究、ADR、设计和实施计划。
- 面向小白的 YouTube 视频/字幕/影视短片指南和 sidecar 字段参考。
- 面向小白的 B 站视频/字幕指南、sidecar 参考与真实首批验收报告。

## 2026-09-08

### 修复与加固

- 下载任务使用原子领取与 lease，避免并发重复领取和启动时误重置活跃任务。
- 加强 URL、DNS、重定向、文件大小、磁盘空间、响应类型和音频流校验。
- 修复 Range 续传与 HTTP 416 的错误完成判定。
- Opus 转码失败不再保留错误扩展名或误报成功。
- 文件名加入稳定标识，内容使用 SHA-256 去重。
- 修复发布时间回填、日期过滤、分组领取和若干来源解析问题。
- 增加单元和集成回归测试。

升级旧数据库时会自动添加缺失列和索引。升级前仍应停止写入进程并使用 SQLite backup API 备份。

## 2026-09-09

### 实测验收

- 在 `dev_L4_1gpus` 完成 5,000 条记录的详细元数据回填与受限扩量。
- 5,000 条记录全部具备 `metadata_json`，其中 4,949 条 rich、51 条 truthful partial、0 条 unresolved。
- 4,775 条音频完成下载，4,713 个物理音频通过全量 ffprobe；62 条内容哈希重复记录只保留一份文件。
- 全部物理音频均有 schema v2 JSON sidecar；无 JSON 解析错误、缺失 sidecar 或签名 URL 泄露。
- 新增面向小白的[已下载数据使用指南](docs/getting-started/using-downloaded-data.md)和[完整终态验收报告](docs/reports/2026-09-09-complete-metadata-and-scale-validation.md)。
- 失败记录按来源保留并脱敏记录原因，没有执行无边界的全库重试。
