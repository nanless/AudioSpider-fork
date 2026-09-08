# 来源适配器

## 统一输出

每个 Spider 最终生成 `AudioRecord`。核心字段包括 URL、来源、标题、格式、大小、时长、语言、分类、节目或说话人、来源 ID 和发布时间。外部站点不一定提供全部字段，空值不一定是程序错误。

## 快速对比

| 名称 | 入口 | 典型格式 | 增量方式 | 主要风险 |
|---|---|---|---|---|
| `podcast_rss` | 固定 RSS | MP3/M4A | RSS 发布时间 | feed 失效或过大 |
| `librivox` | LibriVox API + Archive.org | MP3 | 图书 API | Archive.org 可达性 |
| `xiaoyuzhou` | 种子节目/发现页 | M4A | 节目页 | HTML 结构变化 |
| `ximalaya` | 种子 track ID 附近探测 | MP3/M4A | checkpoint + ID | 非正式分页、元数据启发式 |
| `bilibili` | 关键词搜索和视频分P | M4A | 搜索页/checkpoint | 风控、签名 URL、大文件 |

## `podcast_rss`

优先推荐给新手。它读取 `config.py` 中 `SPIDER_CONFIGS["podcast_rss"]["feeds"]`，解析 RSS `<item>` 的 `<enclosure>`。

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 5
python collect.py --spiders podcast_rss
```

当前保护：

- 只接受 HTTP(S) feed 和媒体 URL。
- RSS 响应默认不超过 20 MiB。
- 按每个 feed 节目上限截断。
- 保存 `published_at`，支持日期过滤。

局限：主要面向 RSS 2.0 播客。非标准 XML、Atom 扩展或需要登录的 feed 可能无法解析。

## `librivox`

从 LibriVox 获取公版有声书目录，再读取 Archive.org 元数据找 MP3。

```bash
python probe.py --source librivox --books 1 --episodes 5
python collect.py --spiders librivox
```

优点是内容边界较清楚；缺点是一本书通常分成多章，而且强依赖 Archive.org 的网络可达性。正式采集的 `max_tracks_per_book` 在 `config.py` 中配置；探针用 `--episodes` 单独限制每书章数。

## `xiaoyuzhou`

从配置的播客页获取节目，也可从站点发现页扩充播客 URL。

```bash
python probe.py --source xiaoyuzhou --feeds 1 --episodes 5
python probe.py --source xiaoyuzhou --feeds 1 --episodes 5 --include-discovery
```

默认探针不启用发现页，以减少访问范围。正式 Spider 依赖页面中的嵌入数据或 HTML，页面改版后应先运行探针。

## `ximalaya`

当前实现以种子 track ID 为中心探测附近 ID，并保存 checkpoint。这不是完整的专辑分页爬取。

```bash
python probe.py --source ximalaya --seeds 2 --probes 6 --probe-range 1
python collect.py --spiders ximalaya
```

主要限制：

- 种子的选择会显著影响样本。
- 语言、分类和标题可能是启发式推断。
- 站点内部 API 或签名规则可能变化。

## `bilibili`

按关键词搜索视频，对每个视频解析分P，选择 DASH 音频地址。来源 ID 包含 BV 号和分P，用于稳定去重。

```bash
python probe.py --source bilibili --keywords "有声书 合集" --search-pages 1 --videos 2 --parts 3
```

主要风险：

- 匿名接口可能限流、风控或返回空数据。
- 媒体 URL 可能带时效签名，长时间 pending 后可能过期。
- 长合集会产生大量分P和大文件。
- 视频可访问不等于已经获得下载或训练授权。

默认有每词页数、每词视频数和每视频分P数上限。先用探针小规模确认，再考虑正式采集。

## 配置与正式注册

`collect.py` 的 `ALL_SPIDERS` 注册当前五个 Spider；`config.py` 的 `SPIDER_CONFIGS` 控制启用状态与来源参数。新增来源时两处都要同步，并为 `probe.py` 增加一个有严格上限的配置分支。

## 新增适配器的契约

1. 继承 `spiders.base.BaseSpider`。
2. 设置稳定且唯一的 `name`。
3. 生成 `AudioRecord`，URL 必须是 HTTP(S)。
4. 为站点实体设置稳定 `source_id`。
5. 支持 `crawl(on_batch=...)`，大批量结果要分批回调。
6. 为页面解析、上限、增量行为和异常情况写测试。
7. 在本页、CLI 参考和根 README 的来源矩阵中更新说明。

更多细节见[开发与测试](../design/development.md)。
