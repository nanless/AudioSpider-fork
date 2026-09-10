# 运行与维护

## 推荐进程布局

从一个采集进程和一个下载进程开始：

```text
collect/discover  →  audiospider.db  →  main.py
```

不要一开始同时运行所有来源、多个 downloader 和高并发 ffmpeg。

不同来源可以各自运行一个 downloader。每个进程会为自己领取的整批任务续租，且只在自己的过滤范围内回收过期任务；因此 B 站与 YouTube 长视频批次不会互相重置状态。不要为同一来源、同一过滤条件重复启动两个进程。

## 启动前检查

```bash
python doctor.py
git status --short --branch
python main.py stats
```

确认没有意外代码改动、磁盘空间充足、数据库完整。

## 建议的后台运行方式

使用 systemd、supervisor、容器或任务平台托管。不要仅靠 SSH 终端中的 `&` 长期运行，否则连接断开后进程状态难以确认。

最小命令示例：

```bash
python discover.py --loop --interval 86400
python main.py --loop --limit 100 --workers 4 --interval 60
```

旧 feed 更新需要单独运行：

```bash
python discover.py --backfill-published
```

## 日志

日志位于 `logs/`。单个长任务日志按 50 MiB 轮转，最多保留 5 个备份。

```bash
ls -lh logs
tail -f logs/download_*.log
tail -f logs/collect_*.log
tail -f logs/discover_*.log
```

## 数据库备份

不要在 SQLite WAL 活跃时只复制 `.db` 文件。推荐使用 SQLite backup API：

```bash
python - <<'PY'
import sqlite3
source = sqlite3.connect('audiospider.db')
target = sqlite3.connect('audiospider-backup.db')
source.backup(target)
target.close()
source.close()
PY
```

验证备份：

```bash
python doctor.py --db audiospider-backup.db
```

## 升级代码

1. 停止 discover/collect/main 进程。
2. 备份数据库。
3. 确认 Git 工作区干净。
4. 拉取代码。
5. 更新 Conda 环境。
6. 执行 doctor 和测试。
7. 小规模 probe 后恢复任务。

```bash
git status --short --branch
git pull --ff-only
bash scripts/bootstrap_conda.sh
python doctor.py
bash scripts/test.sh
python probe.py --source podcast_rss --feeds 1 --episodes 5
```

## 容量规划

- 原始播客常见几十到几百 MB/集。
- B站长合集的单个 DASH 音频可能超过 1 GiB。
- 默认单文件上限为 4 GiB。
- Opus 32 kbps 理论上每小时约 14.4 MB，不含容器开销。
- 转码时 CPU 和临时磁盘会同时增加。

持续运行前监控：

```bash
df -h .
du -sh downloads logs
ps aux | grep -E 'discover.py|collect.py|main.py'
```

## 优雅停止

前台进程使用 `Ctrl-C`，后台进程先发送 SIGTERM，并用精确 PID 确认它已经退出。当前实现
不会承诺收到信号后把正在传输的整个文件写完；已领取行会保留 `downloading/claimed_by`
直到 lease 过期，安全 `.part` 留给下次续传。

必须提前恢复时，不要手写全局 UPDATE。先从只读查询取得已退出进程的精确 `claimed_by`，
再用默认 dry-run 的恢复工具：

```bash
python scripts/recover_download_claims.py \
  --claimed-by 'hostname:pid:nonce' --source bilibili \
  --artifact-kind video_bundle --category 访谈 --language zh
```

核对 JSON 中的全部行后才加 `--apply`。apply 会拒绝本机仍存活的 PID，先用 SQLite Backup
API 创建恢复点，然后只把同一 `claimed_by` 且仍为 `downloading`、同时命中全部过滤器的行
改回 pending；其他 worker、pending 和 failed 均不动。更严格的批次可再用成对
`--id-min/--id-max` 或 `--job-keys-file`。

## 数据保留

- 删除数据库会丢失任务状态和 URL元数据。
- 删除音频不会自动把数据库 done 改回 pending。
- 删除 sidecar 后可运行 `python main.py fix-meta` 对仍存在的 done 文件补建。

涉及删除时先备份并检查目标路径。
