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
