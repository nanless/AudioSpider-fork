# 变更记录

本文件记录面向使用者的重要变化。开发中的细节以 Git 历史为准。

## Unreleased

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
- `youtube_dataset.py`：独立采集 YouTube 长访谈视频、16 kHz WAV、平台人工/自动字幕和完整 sidecar。
- 字幕对齐影视短片：生成可追溯的 MP4/WAV/VTT/TXT/JSON 五件套，严格限制 0.418–29.888 秒。
- YouTube 清单探测、稳定 job/clip ID、目录级 staging、SHA-256/ffprobe/VTT 全量审计。

### 安全

- 背景资产使用公网目标与重定向校验、MIME 白名单、单资产/总量/数量上限。
- sidecar 递归移除 URL 查询参数，避免持久化临时签名。
- 背景资产失败不会影响已验证音频的 done 状态。
- YouTube 仅接受无凭据单视频 URL，不用 Cookie、不绕过访问控制、不保存临时签名 URL；未核权样本明确标成候选。

### 文档

- 新手概念、安装和首次运行。
- 发现采集、下载转码、运维和故障排查。
- CLI、配置、数据库与来源适配器参考。
- 架构、安全、数据流和开发说明。
- 背景信息深度研究、ADR、设计和实施计划。
- 面向小白的 YouTube 视频/字幕/影视短片指南和 sidecar 字段参考。

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
