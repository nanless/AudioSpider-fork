# 故障排查

排查时不要盲目重试全量任务。先确定是环境、网络、站点解析、数据库还是下载阶段出问题。

## 先收集证据

```bash
python doctor.py
python main.py stats
git log -1 --oneline -1
git status --short --branch
df -h .
```

再查看最新日志：

```bash
ls -lt logs | head
tail -n 200 logs/download_*.log
tail -n 200 logs/collect_*.log
tail -n 200 logs/discover_*.log
```

提问或报告故障时，请附上命令、时间、来源、错误日志和 `doctor.py --json` 输出。不要附上 API Secret、Cookie 或完整的签名媒体 URL。

## Conda 和 Python

### `conda: command not found`

```bash
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
```

新机器上运行 `bash scripts/bootstrap_conda.sh`。脚本会先查找现有 Conda，不会自动安装 Miniforge。

### `ModuleNotFoundError`

先确认当前解释器：

```bash
which python
python --version
python -m pip check
```

再同步环境：

```bash
bash scripts/bootstrap_conda.sh
```

不要在系统 Python 和 Conda Python 之间混用 `pip`。

## ffmpeg

### 找不到 ffmpeg/ffprobe

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

重新运行 `python doctor.py`。

### 没有 libopus

```bash
ffmpeg -hide_banner -encoders | grep libopus
```

没有输出表示当前 ffmpeg 构建不能执行 `--format opus`。安装包含 libopus 的 ffmpeg，或临时使用 `--format original`。

## 真实探针

### `probe.py` 返回 `empty`

`empty` 的退出码是 2，表示没有解析到记录，不等于 Python 崩溃。可能原因：

- 站点返回了空列表。
- 匿名 API 被限流或风控。
- 页面字段已变化。
- RSS 本身没有有效 enclosure。
- 服务器所在地区不能访问目标。

先将规模保持在 1–5 条，查日志中的 HTTP 状态和解析警告。

### Archive.org 超时

```bash
curl -I --max-time 15 https://archive.org/
```

如果 TCP 连接超时，这是出口网络问题。反复重跑 LibriVox 不会修复网络，应检查防火墙、代理、DNS 和地区路由。

## 数据库

### `database is locked`

SQLite 支持多读者，但同时只有一个写者。减少同时运行的 `discover.py`/`collect.py`/`main.py` 进程，并检查是否有长时间不退出的手工 SQLite 会话。

```bash
ps aux | grep -E 'discover.py|collect.py|main.py|db_viewer.py'
```

不要删除 `-wal`/`-shm` 文件来“解锁”。

### 完整性检查失败

先停止写入进程，保留现场并复制数据库备份。然后只读检查：

```bash
python doctor.py --db audiospider.db --json
sqlite3 audiospider.db 'PRAGMA integrity_check;'
```

不要在没有备份的情况下手工删表或重建数据库。

## 下载

### 任务一直是 `downloading`

下载任务有 lease。进程异常退出后，没过期的 lease 不会被立即抢走。最安全是等待默认
7200 秒后由同范围领取逻辑回收。必须提前恢复时，先确认精确 PID 已退出，再运行：

```bash
python scripts/recover_download_claims.py \
  --claimed-by 'hostname:pid:nonce' --source youtube \
  --artifact-kind video_bundle --category 访谈 --language en
# 审阅 matched rows 后，原命令追加 --apply
```

不要在活跃 worker 仍存在时手工将状态改回 pending，也不要使用不带 `claimed_by` 的全局
UPDATE；这会造成重复下载或覆盖其他来源的租约。

### 留下 `.part`

`.part` 是断点文件。修复网络后重试任务，下载器会在服务端正确支持 Range 时续传。如果日志显示 Content-Range 不匹配，程序会放弃追加并重新完整下载。

### 磁盘空间不足

```bash
df -h .
du -sh downloads logs tmp
```

默认保留 20 GiB 安全水位。降低水位会增加数据库和文件系统风险，优先增加容量或减小批次。

### 下到 HTML/JSON 错误页

当前下载器会根据 Content-Type、文件签名和 ffprobe 拒绝明显错误页。查日志中是否有登录、防盗链、403、412 或签名过期信息。

### Opus 转码失败

先对原文件运行：

```bash
ffprobe -v error -show_streams /path/to/input
ffmpeg -v error -i /path/to/input -f null -
```

常见原因是源文件不完整、容器与扩展名不匹配、ffmpeg 解码器缺失或临时磁盘不足。

## B站画面字幕 OCR

### 平台没有字幕，但画面明明有字

烧录字不是平台字幕轨。先确认 `caption.status`：`auth_required` 需用户明确授权后重查，不能
直接判为 missing；仍无合法同语言平台轨时再 dry-run OCR：

```bash
python scripts/backfill_bilibili_captions.py --help
/root/miniforge3/envs/audiospider-ocr/bin/python scripts/backfill_bilibili_captions.py --job-key '<完整 job_key>' --limit 1 --visual-ocr
```

OCR 使用本地 `source.mp4`，不需要 Edge/B站 Cookie。只有平台 API 重查才允许将已授权的
最小 Cookie 通过 stdin/一次性内存桥送入带 `--allow-bilibili-cookie` 的当前进程；不要复制
Edge profile，也不要把 Cookie 放在命令行或故障日志里。

### 已在本机 Edge 登录，怎么安全回填平台字幕

1. 在 Edge 正常打开 B站并确认已经登录；不要导出整个浏览器 profile。
2. 用户必须明确授权本轮读取登录态。
3. 本机受控 helper 以只读方式打开 Edge Cookie SQLite，只允许 `.bilibili.com`，只取
   `SESSDATA`、`bili_jct`、`DedeUserID` 等固定字段；通过系统 Keychain 在内存解密。
4. helper 将最小 Cookie header 通过 SSH stdin 送入远端回填进程。Cookie 不能出现在 argv、
   stdout/stderr、临时文件、下载目录、SQLite、sidecar 或 Git；远端 client 构造完成后立即从
   环境移除。
5. 先检查报告的 `done_rows_scanned/candidate_count/completed/failures`，再对仍无平台轨的视频
   做视觉 OCR。`completed=53, failures=0` 只代表该轮候选全部完成，不等于数据库所有 done
   bundle 都有平台字幕。

这是 macOS 本机运维动作，服务器端仓库不会也不应该直接读取本机 Edge profile。若 helper
报告必要 Cookie 缺失/过期，应回到 Edge 刷新登录；不要改成手工复制 Cookie 值。

### OCR 是 `no_stable_text_detected`

这表示在当前固定 ROI、fps、置信度和连续帧条件下没有形成稳定 cue，不证明视频没有文字。
抽查原帧是否为小字、模糊、花字、竖排、遮挡、快闪或字幕位置移动；再按 `--help` 调整
`--ocr-sample-fps`、`--ocr-region`、`--ocr-min-confidence` 或 profile，不要直接降门槛跑全量。

### OCR 有重复、弹幕、台标或人名条

不要手改 VTT 后冒充可复现结果。调整 ROI/最低置信度/profile 后从同一输入哈希重跑；当前
静态覆盖层过滤和跨帧匹配并不能消除所有场景文字。Whisper 可辅助发现口播不一致，但不是
画面文字真值，也不能改变 `text_source=visual_ocr`。

### OCR backend 不可用或 apply 中断

先用 `--help` 确认当前部署支持 `--ocr-engine paddle`，再检查 PaddleOCR、模型、CUDA 和显存。
环境未创建时先运行 `bash scripts/bootstrap_ocr_conda.sh`，然后运行
`/root/miniforge3/envs/audiospider/bin/python doctor.py --ocr`。
引擎失败应出现在报告 `phase=visual_ocr`，不是平台 missing。中断时停止相关 writer，保留
SQLite backup、rollback 目录和报告，然后运行：

```bash
python scripts/audit_media_queue.py
```

核对 MP4/WAV 原哈希、metadata 闭包和 OCR payload 后，再按报告恢复或重跑精确 job。正常
可捕获异常应自动回滚；`SIGKILL`、断电或存储故障没有跨文件系统/SQLite 原子保证。

## 去重与元信息

### `local_path` 是 `dup:<hash>`

这是内容去重，不是错误。该记录的最终 SHA-256 与已完成记录相同，因此不保留第二份物理文件。

### JSON sidecar 缺失

```bash
python main.py fix-meta
```

该命令只会尝试为已完成且物理文件存在的记录补建 JSON。

## 仍然无法定位

用最小可复现命令代替全量命令，例如：

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 1 --timeout 60
python main.py --source podcast_rss --limit 1 --workers 1 --format original
```

保留相应日志和探针 JSON，以便区分可重现的代码缺陷与外部站点变化。
