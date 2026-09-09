# 背景信息与文本资产

## 保存原则

AudioSpider 保存来源公开提供的信息，不把缺失值补造成事实。平台/RSS 文本和未来 ASR 派生文本必须通过 `text_source` 区分。

## 文件布局

```text
episode.m4a
episode.json
episode.description.txt
episode.description.html
episode.cover.jpg
episode.transcript.1.vtt
episode.transcript.1.txt
episode.chapters.1.json
episode.source-text.1.html
episode.source-text.1.txt
```

只有实际存在的资产才会生成。JSON transcript 和 HTML 原文若结构可识别，会额外生成方便检索的纯文本文件。

## Sidecar schema 2

原有 title、source、format、size、duration、language、category、speaker、时间和 SHA-256 字段保持兼容，并新增：

- `webpage_url`、`description`、`author`、`cover_url`。
- `background_metadata`：Spider 采集的版本化 envelope。
- `background_files`：实际文件状态、路径、字节数、MIME 和 SHA-256。
- `original_url_query_redacted`：原 URL 是否移除了查询参数。

`background_metadata` 包含：

- `schema_version`。
- `text_source`。
- `transcript_status`：`provided`、`reference_only` 或 `not_provided`，避免把缺失文本误认为抓取失败。
- `common`：跨来源公共语义。
- `source_data`：按来源命名的原生字段子集。
- `assets`：transcripts、chapters、source_texts。
- `provenance`：来源和采集时间。

## 来源覆盖

| 来源 | 背景 | 文本/辅助资产 |
|---|---|---|
| Podcast RSS | show/episode 描述、作者、人物、分类、网页、封面、许可 | 声明的 transcript 与 chapters |
| 小宇宙 | 单集描述/时间轴、节目、作者、主播、标签、赞助、封面 | 公开页面只有 transcript reference 时仅保存引用 |
| 喜马拉雅 | 专辑、作者、分类、创建时间、授权/付费标记 | 当前无稳定公开 transcript |
| LibriVox | 书籍简介、作者/译者、章节/朗读者、题材、版权年份、封面 | 公版 `url_text_source` |
| B站 | 视频简介、UP主、封面、分P、权限与统计 | player API 公开返回的字幕 |

## 下载模式

```bash
python main.py --background none
python main.py --background metadata
python main.py --background all
```

`all` 是默认值。背景请求失败不会改变成功音频的 done 状态，失败原因会记录在 sidecar。

## 已有音频回填

不要只重新跑搜索并假设旧记录会再次出现。先审计并按正式库中的稳定来源 ID 定向回填：

```bash
python metadata_backfill.py --audit-only --limit 10000
python metadata_backfill.py --limit 10000 --report logs/metadata-backfill.json
```

可以用 `--source bilibili` 单独处理来源，用 `--missing-only` 仅请求还没有版本化元数据的记录。报告分别给出 `rich`、`partial` 和 `unresolved`，因此 `metadata_json` 空壳不会被算作完整覆盖。

回填数据库后，再为已经下载的物理音频生成相邻资产：

```bash
python main.py background --limit 10000 --workers 4 --background all
```

此命令只处理 done 且物理文件存在的记录；`dup:<hash>` 不会生成重复资产。可用 `--source` 限定来源。

## 资源限制

- 单背景资产默认 20 MiB。
- 每条音频所有背景资产默认 50 MiB。
- 每条音频最多 12 个资产。
- 只接受文本、字幕、JSON/XML 和图片类型。
- 所有初始 URL 与重定向均拒绝私网、localhost、link-local 和保留地址。
- sidecar 中 URL 去掉 query 与 fragment；数据库保留下载所需声明。

## 没有 transcript 时

保持 transcript 列表为空或保存来源给出的非下载引用。不要把 description/show notes 当逐字稿。模型 ASR 应作为独立流程，并标记模型、版本、语言、时间戳、置信度和 `text_source=asr`。
