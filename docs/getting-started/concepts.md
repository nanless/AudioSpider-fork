# 核心概念

## 先记住一句话

AudioSpider 把“发现媒体任务”和“下载文件”分开。看到音频 URL 或视频 bundle 任务出现在数据库里，不代表文件已经下载。

## 三个阶段

### 1. 发现播客源

`discover.py` 去 Apple Podcasts 或 Podcast Index 找 RSS 地址，再解析 RSS 中的节目。

```text
搜索关键词 → 播客目录 → RSS 地址 → 节目 URL → 数据库 pending
```

### 2. 从固定来源采集

`collect.py` 运行 `spiders/` 中的适配器，从配置好的 RSS、小宇宙、喜马拉雅、LibriVox、B站和 YouTube 生成统一任务。普通来源写音频任务；B站/YouTube 可写完整 `video_bundle` 任务。

```text
固定来源 → Spider → AudioRecord → 数据库 pending
```

### 3. 下载

`main.py` 从数据库原子领取 pending 任务，下载并校验普通音频或完整视频 bundle，最后标记 done 或 failed。

```text
pending → downloading → done / failed
```

## RSS 是什么

RSS 是播客发布节目的 XML 文件。每个 `<item>` 通常代表一集，`<enclosure>` 里是媒体 URL。RSS 比网页抓取稳定，因此新手应先从固定 RSS 开始。

## SQLite 是什么

SQLite 是一个单文件数据库。项目的 `audiospider.db` 保存 URL、标题、来源、状态、文件路径和哈希。它不是音频文件本身；删除数据库会丢失任务状态，但不会自动删除 `downloads/`。

## Spider 是什么

Spider 是某个来源的适配器。它知道如何把站点响应转换为统一的 `AudioRecord`。不同站点提供的元数据不同，因此语言、分类、时长和发布时间不一定都可靠。

## Probe 和正式采集的区别

`probe.py` 用严格小上限真实访问站点，默认把结果写进 `/tmp`。它用于回答“这个来源现在能不能抓到”。

`collect.py` 和 `discover.py` 会把结果写入正式数据库，供后续下载。

## 原始格式与 Opus

`--format original` 保留源站文件，速度快、CPU 占用低。`--format opus` 会用 ffmpeg 转成单声道 Opus 32 kbps，文件更小，但需要额外 CPU。

Opus 解码器通常报告 48 kHz。即使编码前使用 24 kHz PCM，训练程序也应检查实际采样率并按需要重采样。

## 去重

项目在三个位置避免重复：

1. URL 唯一约束。
2. 来源稳定 ID，例如 B站的 `BV号_p分P`。
3. 下载完成后的最终文件 SHA-256。

内容重复时，数据库仍保留来源记录，但 `local_path` 会写成 `dup:<sha256>`，不会保存第二份物理文件。

## 下一步

理解这些概念后，继续阅读[从零安装](installation.md)和[第一次运行](first-run.md)。
