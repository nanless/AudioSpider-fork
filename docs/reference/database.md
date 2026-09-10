# 数据库结构

AudioSpider 使用仓库根目录下的 `audiospider.db` SQLite 数据库。数据库是任务状态的真实来源，`downloads/` 只保存文件。

## `audio_urls`

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER | 自增主键 |
| `url` | TEXT | 稳定公开页面或媒体 URL；视频多任务可共用 |
| `source` | TEXT | 来源名，如 `podcast_rss` |
| `artifact_kind` | TEXT | `audio` 或 `video_bundle` |
| `job_key` | TEXT | 视频的不可变策略/修订任务键；旧音频为空 |
| `title` | TEXT | 标题 |
| `file_format` | TEXT | 当前格式，如 `mp3`/`m4a`/`opus` |
| `file_size` | INTEGER | 字节数，未知为 0 |
| `duration` | INTEGER | 时长秒数，未知为 0 |
| `language` | TEXT | 语言标识，可为空 |
| `category` | TEXT | 内容分类 |
| `speaker` | TEXT | 主播、节目或朗读者等 |
| `webpage_url` | TEXT | 音频对应的公开网页 |
| `description` | TEXT | 清理后的简介/show notes |
| `author` | TEXT | 作者、UP主、主播或书籍作者 |
| `cover_url` | TEXT | 首选封面 URL |
| `metadata_json` | TEXT | 版本化来源背景信息与资产声明 |
| `status` | TEXT | `pending`/`downloading`/`done`/`failed` |
| `local_path` | TEXT | 完成文件路径或 `dup:<sha256>` |
| `bundle_path` | TEXT | 视频 bundle 完整目录；普通音频为空 |
| `content_hash` | TEXT | 最终物理内容 SHA-256 |
| `source_id` | TEXT | 来源稳定 ID |
| `published_at` | TEXT | 发布时间，ISO 形式字符串 |
| `discovered_at` | TEXT | 发现时间 |
| `downloaded_at` | TEXT | 下载完成时间 |
| `claimed_by` | TEXT | 当前 worker ID |
| `claimed_at` | TEXT | 任务领取时间 |
| `lease_expires_at` | TEXT | lease 过期时间 |

主要索引覆盖 `status`、`source`、`source_id`、`content_hash`、来源/分类/语言+状态、发布时间+状态和 lease 过期时间。

## `crawl_checkpoints`

| 字段 | 类型 | 含义 |
|---|---|---|
| `source` | TEXT | 来源 |
| `checkpoint_key` | TEXT | 检查点名 |
| `checkpoint_value` | TEXT | 检查点值 |
| `updated_at` | TEXT | 更新时间 |

`source + checkpoint_key` 是联合主键。适配器用它保存最新页、时间或其他增量位置。

## `discovered_feeds`

`discover.py` 会按需建立这张表，用于记录通过 Apple/Podcast Index 找到的 RSS、播客名、发现途径、解析时间和节目数。它不是下载队列；解析出的节目仍会进入 `audio_urls`。

## 状态机

```text
                         下载/校验/转码成功
pending  ──原子领取──> downloading ──────────────> done
   ^                         |
   |                         +-- 失败 ----------------> failed
   |                                                          |
   +-------------------- retry-failed ------------------------+
   |
   +-------------------- lease 过期回收 <--- downloading
```

- 领取在 `BEGIN IMMEDIATE` 事务内完成，避免多个 worker 拿到同一任务。
- 只有 lease 过期的 `downloading` 任务会被回收。
- 活跃 downloader 会周期性续租本批已经领取但尚未完成的全部记录；默认每 60 秒一次。
- 过期回收沿用本次领取的来源、类别、语言、时间和产物类型过滤器，不会因启动 B 站任务而改写 YouTube 队列。
- 旧版数据库中没有 lease 的 `downloading` 记录会在迁移时恢复为 pending。
- `retry-failed` 会重新领取 failed 记录，应当设置小批次。

## 去重语义

1. `job_key=''` 的历史/普通音频使用 URL partial unique。
2. 视频使用 `(source, artifact_kind, job_key)` partial unique，同一 URL 的不同画质、字幕或修订任务可共存。
3. 无 `job_key` 的适配器仍用 `(source, source_id, artifact_kind)` 做应用层去重。
4. 普通音频下载完成后使用 `content_hash` 识别实际内容重复；视频 bundle 哈希只用于闭包审计，不会删除主文件。

对第 3 种，记录仍然是 done，但 `local_path` 是 `dup:<sha256>`，不应把它当作文件路径打开。

## 时间语义

- 时间字段以 ISO 字符串存储。
- 旧记录的 `published_at` 可能为空。
- `discover.py --backfill-published` 会重新解析已知 RSS 尝试回填。
- `main.py --before YYYY-MM-DD` 包含指定日期的全天。

## 查看与安全查询

优先使用：

```bash
python main.py stats
python db_viewer.py overview
python db_viewer.py status pending -n 20
python db_viewer.py checkpoints
```

手工查询建议以只读模式打开：

```bash
sqlite3 -readonly audiospider.db \
  'SELECT source,status,COUNT(*) FROM audio_urls GROUP BY source,status;'
```

## 自动迁移

`Storage` 初始化会创建缺失表和索引，并对旧表添加媒体任务、发布时间、
领取/lease 和背景信息列。为去掉旧的全局 URL unique，表会在单个事务中重建，
原 ID、状态和数据全部复制后才删除事务内临时旧表。

即使如此，升级前仍应停止写入并使用 SQLite backup API 备份。

## 禁止操作

- 不要在活跃任务期间删除 `audiospider.db-wal` 或 `audiospider.db-shm`。
- 不要为了“从头开始”直接删数据库。
- 不要在 worker 仍活跃时手工批量修改 status。
- 不要仅复制 `.db` 而忽略正在活跃的 WAL；使用 backup API。
