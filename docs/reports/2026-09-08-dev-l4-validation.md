# dev_L4_1gpus 服务器验收报告（2026-09-08）

## 结论

AudioSpider 在 `dev_L4_1gpus` 的 `/root/code/github_repos/AudioSpider-fork` 上通过环境、离线回归、五来源真实采集探针和已有真实下载文件检查。探针使用 `/tmp` 独立数据库，没有向正式下载队列新增记录。

## 环境

| 项目 | 结果 |
|---|---|
| Conda 环境 | `/root/miniforge3/envs/audiospider` |
| Python | 3.11.16 |
| ffmpeg | 4.2.7 |
| libopus | 可用 |
| Python 依赖 | `pip check` 通过 |
| 可用磁盘 | 约 34.6 TiB |
| Git 基线 | `f9fa1f6 fix: harden crawling and download pipeline` |

`scripts/bootstrap_conda.sh` 在已有环境上成功执行更新，锁定依赖均已满足。

## 离线回归

执行：

```bash
bash scripts/test.sh
```

结果：

- 26 项 unittest 全部通过。
- `ResourceWarning` 按错误处理，未发现泄漏。
- Python 编译通过。
- `pip check` 无损坏依赖。
- doctor 全部检查通过。
- 21 个 Markdown 文件的相对链接检查通过。
- Shell 脚本语法检查通过。

测试覆盖存储领取与 lease、日期/分组过滤、内容去重、URL 与路径安全、私网拦截、响应大小限制、HTML 错误响应拒绝、真实 ffmpeg Opus 转码、离线转换、RSS 成功和失败响应、doctor 和 probe 参数上限。

## 五来源真实探针

同一时间以最小配置并行执行，每个探针有 60 秒总超时：

| 来源 | 限制 | 数据库记录 | 非法媒体 URL | 结果 |
|---|---|---:|---:|---|
| Podcast RSS | 1 feed × 2 集 | 2 | 0 | 通过 |
| 小宇宙 | 1 feed × 2 集 | 1 | 0 | 通过 |
| 喜马拉雅 | 1 种子、2 次探测、最多 2 集 | 2 | 0 | 通过 |
| B站 | 1 关键词页、1 视频、1 分P | 1 | 0 | 通过 |
| LibriVox | 1 本书 × 5 章 | 5 | 0 | 通过 |

Podcast RSS 样例识别出中文、播客分类、发布时间、时长和 HTTPS 媒体主机。B站使用增量回调，因此 `returned_records` 可以是 0，而 `database_records` 和 `inserted_records` 是 1；验收以临时数据库中的记录为准。

第一次 LibriVox 探针只限制书数，一本书产生了 63 章。根据该实测补充了 `max_tracks_per_book`，并让探针用 `--episodes` 限制每书章节。修复后相同的 1 书探针严格返回 5 章，新增回归测试通过。

外部站点具有时效性。本报告证明 2026-09-08 从该服务器可访问和解析，不承诺未来页面、接口、风控或地区网络保持不变。

## 下载与文件验收

离线测试通过本机临时 HTTP 服务完成了 WAV 下载、ffprobe 校验、Opus 转码、SHA-256 和 JSON sidecar 验证。

正式库中已有一条此前完成的真实 Podcast RSS 下载：

| 项目 | 结果 |
|---|---:|
| ffprobe | 通过 |
| 时长 | 770.448 秒 |
| 文件大小 | 12,758,558 字节 |
| JSON sidecar | 可解析 |

正式数据库最终状态：

| 状态 | 数量 |
|---|---:|
| pending | 139 |
| downloading | 0 |
| done | 1 |
| failed | 0 |
| 合计 | 140 |

`doctor.py` 的 SQLite quick check 为 `ok`。真实探针前后正式库记录总数保持 140，证明探针没有污染正式队列。

## 未覆盖范围

- 没有重新下载第二个大型真实音频，避免无必要消耗带宽和磁盘。
- 没有启用 Podcast Index，因为验收环境没有要求注入 API 凭据。
- 没有验证音频版权、训练许可、内容语义质量或说话人标签准确性。
- 没有做多机共享数据库或长期压力测试；当前架构定位为单机优先。

## 建议的日常验收命令

```bash
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
bash scripts/test.sh
python probe.py --source podcast_rss --feeds 1 --episodes 2
python main.py stats
```
