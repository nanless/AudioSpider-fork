# 配置与环境变量

## 配置加载方式

项目启动时直接导入 `config.py`。资源上限和 Podcast Index 凭据从环境变量读取；来源列表、默认延时等当前在 `config.py` 中配置。

项目不会自动读取 `.env`。`.env.example` 只是变量清单。请在 shell、systemd、Docker 或任务平台中注入变量。

## 凭据

| 变量 | 默认 | 用途 |
|---|---|---|
| `PODCAST_INDEX_KEY` | 空 | Podcast Index API Key |
| `PODCAST_INDEX_SECRET` | 空 | Podcast Index API Secret |

只有使用 `discover.py --source podcastindex` 时才需要。

```bash
export PODCAST_INDEX_KEY='...'
export PODCAST_INDEX_SECRET='...'
python discover.py --source podcastindex --keywords news --pi-max-pages 1
```

不要把真实凭据提交到 `.env.example`、README、日志或 Git。

## 资源上限

| 变量 | 默认值 | 影响 |
|---|---:|---|
| `AUDIOSPIDER_MAX_BATCH_SIZE` | `10000` | `main.py --limit` 的上限 |
| `AUDIOSPIDER_MAX_WORKERS` | `32` | `main.py --workers` 的上限 |
| `AUDIOSPIDER_MAX_DOWNLOAD_BYTES` | `4294967296` | 单个媒体最大 4 GiB |
| `AUDIOSPIDER_MIN_DISK_FREE_BYTES` | `21474836480` | 下载期间最小空闲 20 GiB |
| `AUDIOSPIDER_DOWNLOAD_LEASE_SECONDS` | `7200` | 已领取下载任务的 lease |
| `AUDIOSPIDER_MAX_RSS_SIZE` | `20971520` | 单个 RSS 响应最大 20 MiB |
| `AUDIOSPIDER_MAX_BACKGROUND_ASSET_BYTES` | `20971520` | 单个背景资产最大 20 MiB |
| `AUDIOSPIDER_MAX_BACKGROUND_TOTAL_BYTES` | `52428800` | 每条音频背景资产合计最大 50 MiB |
| `AUDIOSPIDER_MAX_BACKGROUND_ASSETS` | `12` | 每条音频最多请求的辅助资产数 |
| `AUDIOSPIDER_BACKGROUND_ASSET_TIMEOUT` | `75` | 单个辅助资产硬超时秒数 |
| `AUDIOSPIDER_YOUTUBE_MAX_ITEMS` | `100` | 单轮最多核验多少个 YouTube 清单项 |
| `AUDIOSPIDER_YOUTUBE_INSPECT_TIMEOUT` | `120` | 单个 YouTube 元数据/字幕核验子进程硬超时（秒） |
| `AUDIOSPIDER_YOUTUBE_DOWNLOAD_TIMEOUT` | `14400` | 单个 YouTube 完整 bundle 下载子进程硬超时（秒） |
| `AUDIOSPIDER_BILIBILI_CONTENT_LANGUAGE` | `zh` | B站受控搜索批次的内容语言；英文批次显式设为 `en` |
| `AUDIOSPIDER_BILIBILI_CATEGORY` | 空 | 可选统一分类覆盖；例如访谈批次设为 `访谈` |
| `AUDIOSPIDER_BILIBILI_KEYWORDS` | `config.py` 列表 | 逗号分隔的本轮 B站搜索词 |
| `AUDIOSPIDER_BILIBILI_REQUIRED_TITLE_TERMS` | 空 | 标题至少命中一个词才保留 |
| `AUDIOSPIDER_BILIBILI_EXCLUDED_TITLE_TERMS` | 空 | 标题命中任一词就排除；仅是候选选择证据 |
| `AUDIOSPIDER_BILIBILI_MAX_SEARCH_PAGES` | `5` | 每个搜索词最多页数 |
| `AUDIOSPIDER_BILIBILI_MAX_VIDEOS_PER_KEYWORD` | `100` | 每个搜索词最多解析 BV 数 |
| `AUDIOSPIDER_BILIBILI_MAX_PAGES_PER_VIDEO` | `200` | 每个 BV 最多入队分 P 数 |
| `AUDIOSPIDER_BILIBILI_MAX_NEW_RECORDS` | `0` | 本轮真实新增任务上限；`0` 表示不额外限制 |
| `AUDIOSPIDER_BILIBILI_MIN_DURATION_SECONDS` | `0` | 单分 P 最短时长 |
| `AUDIOSPIDER_BILIBILI_MAX_DURATION_SECONDS` | `14400` | 单分 P 最长时长 |
| `AUDIOSPIDER_BILIBILI_JOB_ATTEMPTS` | `3` | 单个 B站 bundle 的临时 API/CDN 故障最多尝试次数（1–10） |
| `AUDIOSPIDER_BILIBILI_RETRY_BACKOFF_SECONDS` | `10` | B站单任务线性重试退避基数秒数（0–600） |

所有值必须是正整数字节或秒数。例如：

```bash
export AUDIOSPIDER_MAX_WORKERS=8
export AUDIOSPIDER_MAX_BATCH_SIZE=1000
export AUDIOSPIDER_MAX_DOWNLOAD_BYTES=2147483648
python doctor.py
```

`main.py` 默认使用 4 个 downloader worker；`AUDIOSPIDER_MAX_WORKERS` 是允许用户传入的上限，不是默认并发数。

## 固定路径

`config.py` 以仓库根目录为基准生成：

| 常量 | 相对路径 |
|---|---|
| `DOWNLOAD_DIR` | `downloads/` |
| `LOG_DIR` | `logs/` |
| `DB_PATH` | `audiospider.db` |
| `TMP_DIR` | `tmp/` |

导入配置时会创建 `tmp/` 并在当前进程未设置 `TMPDIR` 时将其指向该目录。

## 网络与重试常量

| 常量 | 默认 | 含义 |
|---|---:|---|
| `MAX_CONCURRENT_DOWNLOADS` | 4 | 默认下载并发 |
| `MAX_CONCURRENT_SPIDERS` | 3 | 爬虫并发参考值 |
| `DOWNLOAD_TIMEOUT` | 600 秒 | 下载超时 |
| `REQUEST_TIMEOUT` | 30 秒 | 普通请求超时 |
| `MAX_RETRIES` | 3 | 重试次数 |
| `RETRY_BACKOFF` | 2.0 | 指数退避基数 |
| `MIN_DELAY` / `MAX_DELAY` | 1 / 3 秒 | 爬取间隔范围 |

这些常量目前需要修改 `config.py` 才能改变。修改后应运行全部测试。

## Spider 配置

`SPIDER_CONFIGS` 按来源保存：

- `enabled`：`collect.py` 是否启用该适配器。
- `discover_urls`：小宇宙种子节目。
- `feeds`：固定 RSS 列表。
- `search_keywords`：B站搜索词等。
- `max_*`：各来源规模上限。

LibriVox 的 `max_items` 限制书数，`max_tracks_per_book` 限制每书章数；两者应配合使用。

修改固定 RSS 示例：

```python
"podcast_rss": {
    "enabled": True,
    "feeds": [
        "https://example.com/podcast.xml",
    ],
    "max_episodes_per_feed": 20,
},
```

添加前先确认 RSS 中的媒体地址允许按你的用途访问。

## 不建议的配置

- 把磁盘安全水位设为极小值。
- 在没有容量测试时将 workers 直接拉到 32。
- 在开发机上使用默认全量发现。
- 把 Cookie、Token 或签名 URL 硬编码进仓库。
- 将 `AUDIOSPIDER_DOWNLOAD_LEASE_SECONDS` 设得短于正常大文件下载时间。
