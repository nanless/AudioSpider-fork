# AudioSpider 5,000 条详细元数据与音频下载验收报告

日期：2026-09-09  
服务器：`dev_L4_1gpus`  
仓库：`/root/code/github_repos/AudioSpider-fork`  
环境：`/root/miniforge3/envs/audiospider`  
验收提交基线：`584fb63 fix: bound background asset requests`

## 1. 执行范围

本轮在已有数据的基础上完成了历史元数据回填、受限扩容到 5,000 条、音频批量下载和终态校验。下载命令为：

```bash
python main.py --limit 5000 --workers 4 --format original --background all
```

下载过程发现一个 RSS 任务在租约过期后长时间保持 `downloading`。已安全停止唯一卡住进程，让过期租约自动回收，并用单条、有界命令继续处理；该条最终按内容哈希去重完成，没有重复保存音频，也没有执行全库 `retry-failed`。

## 2. 数据库与元数据

| 指标 | 结果 |
|---|---:|
| 总记录 | 5,000 |
| `metadata_json` | 5,000 |
| rich | 4,949 |
| truthful partial | 51 |
| unresolved | 0 |
| description | 3,106 |
| author | 5,000 |
| cover_url | 4,979 |
| webpage_url | 4,838 |

`partial` 是来源确实没有提供某些字段，不是空 JSON；所有记录均已完成来源特定的回填尝试。文本来源状态为 `not_provided=4,990`、`reference_only=10`，没有把平台描述或原文误标成 ASR 转写。

按来源的记录数为：`podcast_rss=4,902`、`librivox=40`、`bilibili=27`、`ximalaya=21`、`xiaoyuzhou=10`。

## 3. 音频下载结果

| 状态 | 数量 |
|---|---:|
| done | 4,775 |
| failed | 225 |
| pending/downloading | 0 |
| done 且保留物理文件 | 4,713 |
| 内容哈希去重记录 | 62 |

下载树共约 102.7 GB；数据库统计的音频总时长约 1,657 小时。所有 4,713 个物理音频都有同名 JSON sidecar。

来源失败数：LibriVox 40 条，Podcast RSS 185 条。主要失败类别为：

- Anchor 主机连接失败或 DNS 返回不可用地址；
- Ximalaya 音频地址返回 `text/plain` 而非音频；
- 个别 CDN 返回 HTTP 403；
- 少量历史域名无 DNS 记录。

这些失败均保留在数据库中并记录在下载日志，没有伪造为成功，也没有无限重试。

## 4. 背景资料与安全检查

终态脚本 `/tmp/validate_audiospider_delivery.py` 对下载树进行了全量检查：

| 项目 | 结果 |
|---|---:|
| JSON sidecar | 4,713 |
| sidecar schema 错误 | 0 |
| JSON 解析错误 | 0 |
| 缺失 sidecar | 0 |
| 签名查询参数泄露 | 0 |
| 描述文本 `.description.txt` | 2,874 |
| 安全清理 HTML 描述 | 2,853 |
| 封面文件 | 4,507 |
| transcript 文件 | 0 |
| chapters 文件 | 0 |
| source-text 文件 | 0 |
| 背景资产成功 | 4,507 |
| 背景资产失败 | 185 |

transcript/chapters/source-text 为 0 是本批来源实际没有公开可下载对应文件的结果，不代表音频损坏；sidecar 仍保留声明、来源和 `not_provided` 状态。

## 5. 验证结果

- `PRAGMA quick_check`：`ok`。
- `python doctor.py`：0 个失败、0 个提醒；Python 3.11.16、依赖、ffmpeg、目录和磁盘检查通过。
- 全量 ffprobe：4,713/4,713 通过，无解码错误。
- `bash scripts/test.sh`：52 个单元测试全部通过，依赖检查、编译检查、doctor、Markdown 链接检查（30 个文件）和 Shell 语法检查全部通过。
- Git 工作区在提交报告前保持干净；本报告应作为独立的验收证据提交，不包含数据库、音频、日志或临时文件。

## 6. 可复现入口

```bash
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python metadata_backfill.py --audit-only --limit 10000
python doctor.py
python main.py stats
python db_viewer.py overview
```

下载文件位于 `/root/code/github_repos/AudioSpider-fork/downloads/`，正式数据库为 `/root/code/github_repos/AudioSpider-fork/audiospider.db`。后续若要处理 225 条失败记录，应先按来源逐类人工复核 URL 或重新发现，不建议直接全库重试。
