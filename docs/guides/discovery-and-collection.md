# 发现与采集

## 三个工具的区别

| 工具 | 输入 | 输出 | 写正式数据库 |
|---|---|---|---|
| `probe.py` | 一个受控来源 | 少量样例与临时数据库 | 默认否 |
| `discover.py` | Apple/Podcast Index | 新 RSS 和节目 URL | 是 |
| `collect.py` | 配置中的固定来源 | 节目/音频 URL | 是 |

## 推荐顺序

1. 用 `probe.py` 验证来源当前可用。
2. 用 `collect.py` 测试固定 RSS。
3. 用 `discover.py --source apple_keyword` 小范围扩充。
4. 检查数据库后再下载。

## 真实来源探针

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 5
python probe.py --source librivox --books 1 --episodes 5
python probe.py --source xiaoyuzhou --feeds 1 --episodes 5
python probe.py --source ximalaya --seeds 2 --probes 6
python probe.py --source bilibili --search-pages 1 --videos 2 --parts 3
```

探针只抓元数据，不下载音频。退出码：`0=有结果`、`1=异常`、`2=请求完成但无记录`。

## Apple 发现

```bash
python discover.py \
  --source apple_keyword \
  --keywords 中文播客 \
  --top 10
```

Apple 地区只表示目录地区，不保证节目内容语言。最终语言主要依赖 RSS `<language>`。

## Podcast Index

```bash
export PODCAST_INDEX_KEY="..."
export PODCAST_INDEX_SECRET="..."
python discover.py --source podcastindex --keywords podcast --pi-max-pages 2
```

`--pi-max-pages` 每页可包含大量 feed，第一次不要直接设成 100。

## discover 的三种模式

```bash
# 搜新 feed，只解析此前未成功爬过的 feed
python discover.py --source apple_keyword --keywords 科技播客 --top 20

# 不搜索，只处理数据库里未解析的 feed
python discover.py --parse-only

# 重扫已解析 feed，补发布时间并发现新集
python discover.py --backfill-published
```

feed 超时或返回错误时不会被标记成功，后续可以继续重试。

## 固定来源

```bash
python collect.py --spiders podcast_rss
python collect.py --spiders xiaoyuzhou
python collect.py --spiders librivox
python collect.py --spiders podcast_rss xiaoyuzhou
```

不带 `--spiders` 会顺序运行所有启用的 Spider，不适合作为第一次测试。

## 幂等性

重复运行通常不会重复添加相同 URL/source_id。数据库 URL 有 UNIQUE 约束，采集层也会检查 source_id；已有记录的空 `published_at` 可以被回填。

## 已知边界

- RSS：当前主要支持 RSS 2.0 enclosure。
- LibriVox：API/RSS 可用不等于 Archive.org 媒体从当前服务器可达。
- 小宇宙：页面变化可能导致标题退化为节目名。
- 喜马拉雅：种子附近 ID 探测不是正式专辑分页。
- B站：匿名搜索、风控和时效性 DASH URL 可能变化。

详细字段见[来源适配器参考](../reference/spiders.md)。
