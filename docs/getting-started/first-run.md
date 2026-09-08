# 第一次运行

下面的流程从零开始，并把每一步的影响范围写清楚。

## 1. 确认环境

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
```

## 2. 先做不会访问外网的测试

```bash
bash scripts/test.sh
```

这会运行单元测试、编译检查、依赖检查、doctor 和文档链接检查。

## 3. 真实探测一个 RSS

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 5
```

正常输出中应有：

```json
{
  "result": "ok",
  "database_records": 5,
  "invalid_media_urls": 0
}
```

探针数据库位于输出的 `database` 字段，默认在 `/tmp`。它不会进入正式下载队列。

## 4. 正式采集固定 RSS

```bash
python collect.py --spiders podcast_rss
```

影响：向 `audiospider.db.audio_urls` 添加 pending 记录，不下载文件。

## 5. 检查采集结果

```bash
python main.py stats
python db_viewer.py status pending -n 10
```

先看标题、来源、语言、分类和时长是否符合预期。

## 6. 下载一条

```bash
python main.py --source podcast_rss --limit 1 --workers 1 --format original
```

影响：下载一条真实音频到 `downloads/`，更新状态为 done，并写同名 JSON。

## 7. 验证文件

```bash
python main.py stats
find downloads -type f | head
ffprobe "downloads/实际文件路径.mp3"
```

不要把示例中的“实际文件路径”原样复制，应换成 `find` 输出的真实路径。

## 8. 逐步放大

确认一条成功后：

```bash
python main.py --source podcast_rss --limit 20 --workers 2 --format original
```

再确认稳定后才使用 `--loop`。第一次不要运行默认全量 Apple 分类、全部 B站关键词或无限循环。

## 9. 停止程序

前台运行时按 `Ctrl+C`。正在下载的任务保留 lease，lease 过期后会被重新领取；`.part` 文件可用于 Range 续传。

继续阅读[下载与转码](../guides/download-and-convert.md)。
