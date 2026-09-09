# 2026-09-09 全量详细信息回填与 5,000 条扩量启动报告

## 结论

在 `dev_L4_1gpus` 的 `/root/code/github_repos/AudioSpider-fork` 上，历史数据详细信息已完成非破坏性回填，正式库已按三层硬上限扩到恰好 5,000 条。全部记录都有版本化元数据，`unresolved=0`。剩余 4,755 条新音频已由唯一后台进程开始下载，默认同时保存描述、封面和来源公开的文本资产。

## Git 与测试

相关提交：

- `85ccd15 feat: preserve public audio background metadata`
- `a5db856 feat: backfill historical metadata deterministically`
- `776a214 fix: recover metadata for rolled-off episodes`
- `580713e feat: skip unavailable feeds during bounded discovery`

部署前后分别运行完整 `scripts/test.sh`。随着新增回归用例，最后一轮为 52/52 通过；依赖检查、Python 编译、Doctor、Shell 语法和 29 个 Markdown 文件链接检查均通过。

## 数据库保护与历史回填

迁移前使用 Python `sqlite3.Connection.backup()` 创建：

```text
backups/audiospider-20260909-before-complete-metadata.db
```

原库和备份均通过 `PRAGMA quick_check`。迁移仅新增 `webpage_url`、`description`、`author`、`cover_url` 和 `metadata_json`。

历史回填从 244/244 unresolved 开始。第一轮按来源 ID 匹配 150 条；重新解析已知历史 feed 后达到 221 条；最后通过 LibriVox 标题查询和 RSS 滚动 feed 的显式历史标记达到 245/245 有元数据、0 unresolved。

历史终态为 220 rich、25 partial。partial 包括 21 条喜马拉雅记录（源站公开响应没有简介/封面）以及 4 条已经从 NPR 滚动 feed 移出的旧小时快讯。后者只继承节目级事实，并标记 `historical_item_not_in_current_feed=true`，没有复制其他单集的描述。

## 既有物理音频回填验证

当时 192 条 done 中有 188 个物理音频和 4 条内容哈希去重记录。运行 `main.py background` 后：

| 项目 | 数量 |
|---|---:|
| 物理音频 | 188 |
| schema 2 sidecar | 188 |
| `description.txt` | 160 |
| `description.html` | 137 |
| 封面 | 167 |
| 背景资产失败 | 0 |
| JSON 解析失败 | 0 |
| 缺失 sidecar | 0 |
| 签名查询串泄露 | 0 |
| ffprobe 失败 | 0 |

当时来源没有提供可下载 transcript 或 chapters；小宇宙 10 条只有 `reference_only`。文件数为零不是漏抓，也没有运行 ASR。

## 扩量过程

使用 Apple 关键词发现 RSS，所有正式解析均同时设置 `--max-feeds`、`--episodes-per-feed` 和 `--max-new-records`：

1. 小批门禁新增 39 条，并由历史 feed 更新额外加入 1 条；40/40 待下载记录都有描述、作者、封面和元数据。
2. 中批硬上限新增 2,000 条，数据库达到 2,284 条。
3. 消费剩余可用 feed 新增 1,020 条，达到 4,141 条。
4. 新关键词批次精确新增 859 条，达到 5,000 条后停止。

已证实超时或异常的 Anchor、Spreaker、SoundOn、Buzzsprout、RSS.com、Patreon 和 RFI 等 host 只在当前批次通过 `--exclude-feed-host` 跳过，没有从 `discovered_feeds` 删除。

## 5,000 条元数据终态

| 指标 | 数量 |
|---|---:|
| 总记录 | 5,000 |
| 有 `metadata_json` | 5,000 |
| rich | 4,949 |
| partial | 51 |
| unresolved | 0 |
| 有 description | 3,106 |
| 有 author | 5,000 |
| 有 cover URL | 4,979 |
| 有 webpage URL | 4,838 |
| `not_provided` transcript | 4,990 |
| `reference_only` transcript | 10 |

缺少 description/webpage 的记录保留来源实际空值，不把节目标题或其他单集文字伪装成描述。

## 下载任务

先完成一条 11.1 MiB 新记录的生产烟测，音频、sidecar 与背景信息成功。随后启动唯一长期任务：

```text
PID: 2234470
日志: logs/scale5000_download_20260909.log
marker: tmp/scale5000_download.started
命令: main.py --limit 5000 --workers 4 --format original --background all
```

启动后 34 秒已有 6 条成功，SQLite `quick_check=ok`。任务由 15 分钟心跳只读监控；不会并发启动第二个 downloader，也不会自动进行全库失败重试。终态后将执行全部 sidecar 解析、签名扫描和物理音频 ffprobe，并写最终报告。
