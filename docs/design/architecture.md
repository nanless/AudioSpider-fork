# 系统架构

## 目标与边界

AudioSpider 是单机优先的采集流水线。它把“发现地址”和“传输大文件”解耦，通过 SQLite 在不同命令之间交接任务。它不是分布式调度平台、通用浏览器爬虫或数据授权系统。

## 组件图

```text
外部目录/API/RSS
       |
       v
discover.py --------+
                    |
spiders/* -> collect.py ----> Storage / SQLite <---- db_viewer.py
                    |                |
probe.py -> 临时 DB + JSON           v
                              main.py / Downloader
                                      |
                         +------------+-------------+
                         v                          v
                    downloads/音频             同名 JSON sidecar
```

## 入口层

- `discover.py`：Apple/Podcast Index 目录发现与 RSS 解析。
- `collect.py`：编排固定来源 Spider。
- `main.py`：统计、修复 sidecar 或消费下载任务。
- `probe.py`：对单个 Spider 做有界真实测试。
- `doctor.py`：本机环境与数据库诊断。
- `db_viewer.py`：人类可读的数据库浏览。

入口脚本负责参数校验和编排，不应复制存储或下载核心逻辑。

## 来源层

`spiders/base.py` 定义公共 Spider 行为；各来源模块把不同响应转换为统一 `AudioRecord`。批次回调允许结果边产生边入库，避免大列表长期占内存，也减少中途失败时的数据损失。

站点适配代码属于高变化区域。稳定边界是 `AudioRecord` 和 `crawl(on_batch=...)`，而不是外部 HTML 或匿名 API 的字段。

## 存储层

`storage.py` 负责：

- 建表和加法式迁移。
- URL、来源 ID 和内容哈希去重。
- 分组和日期过滤。
- 事务内原子领取。
- lease 过期回收。
- 下载完成、失败和元数据更新。

SQLite 使用 WAL。当前设计适合一台机器上的少量采集/下载进程；若写入竞争、任务规模或跨机调度成为瓶颈，应先形成迁移设计，不要仅通过启动更多进程扩容。

## 下载层

`downloader.py` 负责：

1. 校验目标 URL 与重定向。
2. 生成安全文件名与安全路径。
3. 检查磁盘和预计文件大小。
4. 使用 `.part` 与 Range 续传。
5. 校验响应、长度和媒体。
6. 可选调用 ffmpeg 转 Opus。
7. 计算最终 SHA-256。
8. 原子落盘、写 JSON，并提交数据库状态。

`main.py` 控制任务批次、过滤、并发和循环；`Downloader` 不负责发现新 URL。

## 工具层

- `convert_audio.py`：离线批量 Opus 转码。
- `opus_to_wav.py`：为下游处理导出 WAV。
- `network_safety.py`：集中实现目标地址和解析结果的网络安全校验。
- `anti_crawler.py`：User-Agent、请求头、延时与代理辅助。
- `scripts/check_docs.py`：保证文档相对链接可达。
- `scripts/test.sh`：统一验证入口。

## 设计原则

- 小批量优先：探针和示例必须有明确上限。
- 数据库先行：状态更新与文件落盘必须可追踪。
- 失败可恢复：续传、lease、failed 重试和 checkpoint。
- 默认安全：限制大小、并发、磁盘水位和网络目标。
- 外部事实可变：Spider 失败必须保留证据，不把当前站点行为当永久契约。
- 文档与代码同步：新增参数、字段或来源时同步更新对应层级文档。

## 何时需要重新设计

出现以下任一情况时，不应继续在现有单机模型上堆补丁：

- 多台服务器必须共享任务。
- SQLite 写锁持续成为吞吐瓶颈。
- 需要严格的租户、权限或审计隔离。
- 需要数百万级活跃任务的调度与优先级。
- 需要对象存储事务、生命周期和跨区域复制。
- 需要浏览器登录态或集中式凭据服务。

此时应评估队列、服务型数据库、对象存储和独立 worker，但迁移仍需保持当前状态机和幂等语义。
