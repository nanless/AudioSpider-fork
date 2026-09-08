# CLI 命令参考

所有命令都应在仓库根目录和 `audiospider` Conda 环境中运行。本页是速查表；最终以 `python <script> --help` 和当前代码为准。

## `doctor.py`

检查 Python、依赖、ffmpeg/libopus、目录写权限、磁盘和 SQLite。默认不访问外网，数据库以只读模式打开。

```bash
python doctor.py
python doctor.py --json
python doctor.py --strict
python doctor.py --db /path/to/db --download-dir /path/to/downloads
```

| 参数 | 含义 |
|---|---|
| `--json` | 输出机器可读 JSON |
| `--strict` | 将“提醒”也当作非零退出 |
| `--db PATH` | 指定待检查数据库 |
| `--download-dir PATH` | 指定数据目录 |

## `probe.py`

用小上限真实访问单个来源。默认写入 `/tmp` 临时数据库，不下载媒体。

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 5
```

通用参数：

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--source` | 必填 | `podcast_rss`、`librivox`、`xiaoyuzhou`、`ximalaya` 或 `bilibili` |
| `--timeout` | 120 | 整个探针的超时秒数 |
| `--samples` | 3 | 输出的脱敏样例数 |
| `--db` | 临时库 | 可选持久化路径 |

来源参数：

| 参数 | 适用来源 | 默认 |
|---|---|---:|
| `--feeds` | RSS、小宇宙 | 1 |
| `--episodes` | RSS、小宇宙、喜马拉雅、LibriVox 每书章数 | 5 |
| `--books` | LibriVox | 1 |
| `--seeds` | 喜马拉雅 | 2 |
| `--probes` | 喜马拉雅 | 6 |
| `--probe-range` | 喜马拉雅 | 1 |
| `--keywords` | B站 | `有声书 合集` |
| `--search-pages` | B站 | 1 |
| `--videos` | B站 | 2 |
| `--parts` | B站 | 3 |
| `--include-discovery` | 小宇宙 | 关闭 |

退出码：0=有记录，1=异常，2=无记录。

## `collect.py`

运行固定来源 Spider，将 URL 写入正式数据库，不下载文件。

```bash
python collect.py
python collect.py --spiders podcast_rss bilibili
python collect.py --spiders podcast_rss --loop --interval 3600
```

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--spiders [NAMES...]` | 所有已启用 Spider | 指定一个或多个来源 |
| `--loop` | 关闭 | 持续采集 |
| `--interval` | 3600 | 循环间隔秒数，必须大于 0 |

合法 Spider 名：`xiaoyuzhou`、`ximalaya`、`librivox`、`podcast_rss`、`bilibili`。

## `discover.py`

在 Apple Podcasts 或 Podcast Index 发现 RSS，然后解析节目到正式数据库。

```bash
python discover.py --source apple_keyword --keywords 中文播客 --top 10
python discover.py --source apple_genre --top 20
python discover.py --parse-only
python discover.py --stats
```

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--keywords [WORDS...]` | 内置词表 | 自定义搜索词 |
| `--top N` | 200 | Apple 每词最多播客，上限 200 |
| `--source [SOURCES...]` | all | 选择发现来源 |
| `--pi-max-pages N` | 20 | Podcast Index recent feeds 最大页数，上限 1000 |
| `--loop` | 关闭 | 持续发现 |
| `--interval` | 86400 | 循环秒数 |
| `--parse-only` | 关闭 | 仅解析未解析的已知 feed |
| `--backfill-published` | 关闭 | 重解析旧 feed，回填发布时间 |
| `--list-feeds` | 关闭 | 列出已发现 feed |
| `--stats` | 关闭 | 显示 feed 统计 |

`--source` 可选值：`apple`、`apple_keyword`、`apple_genre`、`podcastindex`、`all`。Podcast Index 需要凭据。

## `main.py`

从数据库领取任务并下载。

```bash
python main.py [download|stats|fix-meta] [参数]
```

动作：

| 动作 | 含义 |
|---|---|
| `download` | 默认，下载任务 |
| `stats` | 显示总体与分来源状态 |
| `fix-meta` | 为已完成的本地文件补建 JSON |

下载参数：

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--source NAME` | 全部 | 过滤来源 |
| `--category NAME` | 全部 | 过滤分类 |
| `--language CODE` | 全部 | 过滤语言 |
| `--per-source` | 关闭 | 每个来源各取 limit 条 |
| `--per-category` | 关闭 | 每个分类各取 limit 条 |
| `--limit N` | 50 | 单批任务数 |
| `--workers N` | 4 | 下载并发 |
| `--format` | `opus` | `opus` 或 `original` |
| `--retry-failed` | 关闭 | 重新领取 failed |
| `--loop` | 关闭 | 持续消费 |
| `--interval` | 60 | 循环秒数 |
| `--since DATE` | 不限 | 发布时间下界 |
| `--before DATE` | 不限 | 发布时间上界，包含该日 |

`--per-source` 与 `--per-category` 互斥。

## `db_viewer.py`

查看数据库。打开旧版数据库时，该工具可能自动补加 `published_at` 列，因此严格的只读审计请使用 `sqlite3 -readonly`。

```bash
python db_viewer.py
python db_viewer.py overview
python db_viewer.py all -n 20
python db_viewer.py source podcast_rss -n 20
python db_viewer.py status failed -n 20
python db_viewer.py category 播客 -n 20
python db_viewer.py language zh -n 20
python db_viewer.py months 2026-01-01 --before 2026-12-31
python db_viewer.py month 2026-09 -n 50
python db_viewer.py published
python db_viewer.py categories
python db_viewer.py id 1
python db_viewer.py checkpoints
python db_viewer.py duration
```

`-n` 或 `--limit` 限制详情条数；`--before` 用于时间浏览。

## `convert_audio.py`

将已有非 Opus 文件离线转成 Opus，并更新 sidecar 和可匹配的数据库记录。

```bash
python convert_audio.py --dry-run
python convert_audio.py --dir downloads/podcast_rss --workers 4
```

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--dir` | `downloads/` | 扫描目录 |
| `--dry-run` | 关闭 | 只预览 |
| `--workers` | 4 | 转码并发 |

## `opus_to_wav.py`

将 Opus 转为 24 kHz、单声道、PCM16 WAV。

```bash
python opus_to_wav.py INPUT --output-dir DIR --recursive --workers 4
python opus_to_wav.py INPUT --dry-run
```

`input` 可为文件或目录；`--recursive`/`-r` 递归；未给 `--output-dir` 时输出到源文件所在目录。

## 包装脚本与 Make

```bash
bash scripts/bootstrap_conda.sh
bash scripts/test.sh
bash scripts/probe.sh podcast_rss --feeds 1 --episodes 5
make help
make doctor
make test
make stats
make probe-rss
```

`scripts/probe.sh` 的第一个参数是来源，其余参数原样传给 `probe.py`。
