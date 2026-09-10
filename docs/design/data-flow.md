# 数据流与状态机

## 发现和采集

```text
外部响应
   |
   v
解析与字段归一化
   |
   v
AudioRecord 批次
   |
   +-- URL 已存在 ----------> 跳过或回填 published_at
   |
   +-- source_id 已存在 ----> 跳过或回填
   |
   +-- 新记录 --------------> audio_urls(status=pending)
```

Spider 的 `on_batch` 回调让 `collect.py` 在每批产生后立即提交。`discover.py` 还维护 `discovered_feeds`，将目录发现和 RSS 节目解析分成两层。

## 下载领取

`Storage.claim_pending` 或分组领取方法在事务中：

1. 回收 lease 已过期的 `downloading`。
2. 按过滤条件选择 `pending` ID。
3. 为这些 ID 写入 `downloading`、worker ID、领取时间和过期时间。
4. 提交事务后返回任务。

因此多 downloader 可共享一个数据库，但 SQLite 仍只有单写者。并发不是越高越好。

## 文件写入

```text
pending
  |
  v
downloading -> URL/磁盘检查 -> HTTP 响应
                                 |
                                 v
                       .part 写入或安全续传
                                 |
                         长度和媒体校验
                                 |
                    +------------+------------+
                    |                         |
              format=original           format=opus
                    |                         |
                    +----------> 最终文件 <---+
                                 |
                           SHA-256 去重
                                 |
                    +------------+------------+
                    |                         |
              保存音频+JSON              删除重复文件
              local_path=路径             local_path=dup:hash
                    |                         |
                    +----------> done <-------+
```

任一下载、校验或转码错误都会将任务更新为 `failed`，并清除领取字段。日志保存详细错误。

## 原子性边界

- 数据库领取是事务原子操作。
- 媒体先写 `.part`，完成后再成为最终文件。
- Opus 转码先写临时输出，成功后替换。
- 状态只有在文件校验、哈希和最终路径确认后才提交 done。
- JSON sidecar 与数据库不是同一个事务，所以提供 `main.py fix-meta` 修复缺失 sidecar。

## 时间过滤

`published_at` 为空的旧记录不会自然满足所有日期比较。需要按发布时间筛选前，可运行 `discover.py --backfill-published`，并用 `db_viewer.py published` 检查覆盖率。

`--before YYYY-MM-DD` 按结束日期全天处理。多个过滤条件是叠加关系；`--per-source` 或 `--per-category` 只改变每组 limit，不取消其他过滤。

## 增量与幂等

- URL 唯一确保相同地址重复采集不会新增。
- 来源 ID 让签名 URL 变化时仍可识别同一内容实体。
- checkpoint 记录来源爬取位置。
- 内容 SHA-256 处理不同 URL 指向同一字节内容。
- 重复运行 `collect.py` 应主要产生 0 新增，而不是无限复制任务。

## 恢复场景

| 中断位置 | 遗留状态 | 恢复方式 |
|---|---|---|
| Spider 请求前 | 无 | 重新运行 |
| 批次产生后、入库前 | 该批未保存 | 重新运行，依靠幂等去重 |
| 已领取、下载前 | downloading + lease | lease 过期后回收 |
| 下载中 | `.part` | 重新领取后尝试 Range |
| 转码中 | 临时文件 | 下次清理/重新转码 |
| 音频完成、JSON 缺失 | done + 文件 | `main.py fix-meta` |
| 下载校验失败 | failed | 排查后 `--retry-failed` |

## 可观测性

可从三个方向交叉确认：

- 数据库：`main.py stats`、`db_viewer.py`。
- 文件：`find downloads`、`ffprobe`、sidecar JSON。
- 日志：`logs/download_*`、`collect_*`、`discover_*`。

只看“命令退出码为 0”不足以证明数据正确；应同时核对记录数、样例字段、音频探测和状态。

## YouTube 层级数据流

YouTube 数据不用 SQLite 音频队列：

```text
来源 JSON
   |
   +--> inspect --> 标题/时长/频道/字幕轨 --> 本次 inspect JSONL
   |
   +--> download --> 唯一 staging 目录
                        |
                        +--> source.mp4
                        +--> audio.wav
                        +--> captions.*.vtt/.txt
                        +--> metadata.json（bundle commit marker）
                        |
                        +--> 全套成功后目录级 rename
                                      |
                          +-----------+------------+
                          |                        |
                 interviews/              screen_sources/
```

同一视频的 profile、请求字幕语言或 `source_revision` 不同，会得到不同 `job_key`，避免互相覆盖。每次 inspect/download 都写一个新的带 run ID 清单；bundle 本身用稳定 job key 保证重跑幂等。正式流程到父视频为止，不继续生成 `screen_clips/`。

## B 站视频层级数据流

```text
BV 清单 -> view API -> 分 P/CID
                     |
                     +-> player API -> 字幕可用性和逐轨来源
                     |
                     +-> playurl API -> video DASH + audio DASH
                                           |
                                identity-bound .part 续传
                                           |
                               ffmpeg 精确合并 MP4 + WAV
                                           |
                          原始字幕 JSON -> VTT + TXT（有轨道时）
                                           |
                                全闭包校验后提升 parents/
```

`need_login_subtitle=true` 与“公开无字幕”是两种状态；网络/API 失败不允许伪装成任一状态。签名 URL 只在本次请求内使用，resume 绑定无签名的表示层指纹。
