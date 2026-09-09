# 下载与转码

## 下载前检查

```bash
python doctor.py
python main.py stats
python db_viewer.py status pending -n 10
```

确认 pending 记录的来源、语言、分类、时长和文件大小符合预期。

## 小批量下载

```bash
python main.py --limit 5 --workers 1 --format original
```

默认 `--background all`：音频成功后 best-effort 保存描述、封面、公开 transcript、章节和公版原文。背景资产失败只写入 sidecar 状态，不会把已验证音频改为 failed。

| 模式 | 行为 |
|---|---|
| `none` | 不写新的背景文件 |
| `metadata` | 写扩展 JSON 与 description 文本/HTML，不请求辅助 URL |
| `all` | 在 metadata 基础上下载受限的公开辅助资产，默认 |

常用过滤：

```bash
python main.py --source podcast_rss --limit 20
python main.py --language zh --limit 20
python main.py --category 教育 --limit 20
python main.py --since 2026-01-01 --before 2026-12-31 --limit 20
```

`--before YYYY-MM-DD` 包含结束日期全天。

## 按组公平下载

```bash
python main.py --per-source --limit 10
python main.py --per-category --limit 5
```

两个参数互斥。它们仍然保留 source、category、language 和日期过滤条件。

## original 和 opus

| 格式 | 行为 | 优点 | 代价 |
|---|---|---|---|
| `original` | 保留 MP3/M4A/WAV 等源格式 | 快、CPU低、保真源文件 | 格式不统一 |
| `opus` | 转为单声道 Opus 32 kbps | 体积小、训练集格式更统一 | 消耗 CPU，解码通常报告 48 kHz |

```bash
python main.py --limit 20 --workers 2 --format original
python main.py --limit 20 --workers 2 --format opus
```

转码失败会把任务标记为 `failed`，不会再把原格式误报为 Opus 成功。

## 断点续传

下载先写入 `.part`。重新领取任务后，如果 `.part` 已存在，客户端发送 `Range: bytes=N-`。只有服务端返回匹配的 `Content-Range` 才会追加；HTTP 416 不再直接当作成功。

完成前还会检查：

- 检查磁盘安全水位。
- 检查 Content-Length 和实际流式字节数。
- 拒绝 HTML/JSON 错误页。
- 用 ffprobe 检查至少一个音频流。
- 计算最终文件 SHA-256。

## 内容去重

两个 URL产生相同最终文件时，只保留第一份物理文件。第二条数据库记录会写：

```text
local_path = dup:<sha256>
status = done
```

## 持续下载

```bash
python main.py --loop --limit 100 --workers 4 --interval 60
```

默认 lease 为两小时。多个下载进程可以原子领取不同任务，但仍建议从单进程开始，观察 CPU、网络和磁盘后再扩展。

## 重试失败任务

```bash
python main.py --retry-failed --limit 100
python main.py --retry-failed --source bilibili --limit 100
```

失败原因位于日志。不要在没有排查根因时无限循环重试。

## 离线批量转码

预览：

```bash
python convert_audio.py --dry-run
```

执行：

```bash
python convert_audio.py --workers 4
python convert_audio.py --dir downloads/podcast_rss --workers 2
```

工具会更新同名 JSON，并在默认数据库中把匹配的 `local_path/file_format/file_size/content_hash` 更新为最终 Opus。

## Opus 转 WAV

```bash
python opus_to_wav.py downloads/podcast_rss --recursive --output-dir wav-output
```

输出为 24 kHz、单声道、PCM16 WAV，并保留输入的相对目录结构，避免同名文件覆盖。

## 检查结果

```bash
python main.py stats
find downloads -type f -name '*.part'
ffprobe -v error -show_streams /path/to/audio
```

正常完成后不应遗留 `.part`。如果存在，说明下载被中断或失败；先看日志，再决定重试。

## 为已下载文件补背景信息

先对全部历史记录做可量化审计和定向回填，再生成相邻文件；不需要重下已有音频：

```bash
python metadata_backfill.py --audit-only --limit 10000
python metadata_backfill.py --limit 10000 --report logs/metadata-backfill.json
python main.py background --limit 10000 --workers 4 --background all
```

可用 `--source podcast_rss` 等筛选来源。详细文件、来源差异和真实性标记见[背景信息与文本资产](../reference/background-metadata.md)。
