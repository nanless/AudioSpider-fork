# 查看已经下载的音频、视频、字幕和详细背景信息

这篇文档解决两个最常见的问题：

1. 数据库里有 5,000 条，为什么磁盘上不是 5,000 个音频文件？
2. 音频旁边的 JSON、简介、封面和文本文件分别是什么？

文中的命令都在仓库目录执行：

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
```

## 一、三种“数量”不要混淆

AudioSpider 同时保存队列记录和物理文件，所以需要区分：

| 概念 | 含义 |
|---|---|
| 数据库记录 | 已发现的音频 URL 和详细元数据，一条记录对应一个来源项目 |
| `done` | 已经验证为可播放音频的记录 |
| 物理音频 | `downloads/` 中真实存在的 mp3/m4a/aac 等文件 |
| `dup:<sha256>` | 音频内容与另一条记录完全相同，只保留一份物理文件 |
| `failed` | 当前 URL 下载失败，记录和元数据仍保留，方便以后按来源复核 |

因此，`done` 可能大于物理音频数：重复内容不会浪费磁盘。以 2026-09-09 的验收为例，5,000 条记录中有 4,775 条 `done`，其中 62 条通过 SHA-256 内容哈希去重，最终保留 4,713 个物理音频。

## 二、文件在哪里

默认目录结构如下：

```text
AudioSpider-fork/
├── audiospider.db                 # SQLite 队列和元数据
├── downloads/                     # 音频、sidecar 和公开背景文件
│   ├── podcast_rss/科技/
│   │   ├── episode.m4a
│   │   ├── episode.json
│   │   ├── episode.description.txt
│   │   ├── episode.description.html
│   │   └── episode.cover.jpg
│   ├── bilibili/访谈/<source-id>/<job-key>/
│   │   ├── source.mp4
│   │   ├── audio.wav
│   │   ├── captions.*.json/.vtt/.txt
│   │   ├── visual_ocr.json/.vtt/.txt
│   │   └── metadata.json
│   ├── youtube/访谈/<source-id>/<job-key>/
│   │   ├── source.mp4
│   │   ├── audio.wav
│   │   ├── captions.*
│   │   └── metadata.json
│   ├── xiaoyuzhou/
│   └── ximalaya/
├── logs/                          # 运行日志和验收 JSON
└── tmp/                           # 临时文件，不是最终数据
```

只列出真实音频：

```bash
find downloads -type f \( \
  -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.aac' -o \
  -iname '*.opus' -o -iname '*.ogg' -o -iname '*.wav' \
\) | sort | less
```

只列出 B站、YouTube 的完整视频：

```bash
find downloads/bilibili downloads/youtube -type f -name source.mp4 | sort | less
```

B站平台字幕看 `metadata.json` 的 `caption.status/tracks`；画面烧录字幕 OCR 看
`derived_text.visual_ocr.status`。它们是两套独立来源，不能只凭目录里有 `.txt` 就猜字幕类型。
数据库—目录统一审计使用：

```bash
python scripts/audit_media_queue.py
```

查看某个目录的大小：

```bash
du -sh downloads
du -h --max-depth=2 downloads | sort -h | tail -30
```

不要把 `.part` 文件当成可用音频；它们是中断下载留下的临时分片。正常完成后 `.part` 应为 0 个。

## 三、JSON sidecar 里有什么

每个物理音频旁边都有同名 `.json`。这是 `schema_version=2` 的版本化 sidecar，包含：

- 基础字段：标题、来源、来源 ID、原始 URL、格式、大小、时长、语言、分类、发布时间；
- 详细字段：简介、作者/主播、封面地址、原网页地址；
- `source_data`：RSS、B站、小宇宙、喜马拉雅或 LibriVox 的来源特有字段；
- `background_files`：封面、描述、字幕、章节、公版原文的本地路径、SHA-256 和状态；
- `provenance`：采集时间和文本来源标记。

查看单个 sidecar：

```bash
python -m json.tool \
  'downloads/podcast_rss/科技/某个文件.json' | less
```

批量检查 sidecar 是否能解析：

```bash
python - <<'PY'
import json
from pathlib import Path

errors = []
for path in Path('downloads').rglob('*.json'):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        errors.append((str(path), str(exc)))
        continue
    if isinstance(value, dict) and 'file_format' in value:
        if value.get('schema_version') != 2:
            errors.append((str(path), '不是 schema_version=2'))
print(f'检查完成：错误 {len(errors)} 个')
for item in errors[:20]:
    print(item)
raise SystemExit(1 if errors else 0)
PY
```

## 四、描述、封面和文本分别代表什么

### 描述文本

`.description.txt` 是平台或 RSS 提供的简介的纯文本版本；`.description.html` 是经过安全清理的 HTML 版本。它们是节目背景资料，不是对音频重新识别得到的 ASR 文本。

### 封面

`.cover.jpg`、`.cover.png` 等是来源公开提供的封面。sidecar 会记录原始封面地址和本地文件哈希；临时签名查询参数不会被持久化。

### transcript、chapters 和 source-text

只有来源公开提供并且允许下载时，才会出现：

- `*.transcript.*`：来源发布的字幕/文字稿；
- `*.chapters.*`：来源发布的章节时间点；
- `*.source-text.*`：LibriVox 等来源公开的原文。

这些文件的来源会标成 `platform`、`rss` 或 `reference_only`，不会标成 `asr`。如果没有这些文件，表示来源没有公开可下载文本，不代表下载失败。需要机器转写时，应另行设计和运行 ASR 流程，不能把简介冒充转写。

## 五、用 SQLite 查询数据

查看总数和状态：

```bash
python main.py stats
```

只读查询示例：

```bash
sqlite3 -readonly audiospider.db <<'SQL'
.headers on
.mode column
SELECT source, status, COUNT(*) AS n
FROM audio_urls
GROUP BY source, status
ORDER BY source, status;

SELECT id, source, title, author, webpage_url
FROM audio_urls
WHERE status='done'
ORDER BY id DESC
LIMIT 20;
SQL
```

查看详细元数据覆盖率：

```bash
python metadata_backfill.py --audit-only --limit 10000
```

验收时应重点看：`total`、`metadata`、`rich`、`partial` 和 `unresolved`。`unresolved` 应为 0；`partial` 可以存在，因为某些来源确实不提供简介、封面或网页地址。

## 六、确认音频真的能播放

抽查一个文件：

```bash
ffprobe -v error \
  -show_entries format=duration,size \
  -of default=noprint_wrappers=1 \
  'downloads/podcast_rss/科技/某个文件.m4a'
```

全量检查时使用仓库验收脚本（会检查 SQLite、sidecar、签名泄露和全部音频）：

```bash
python /tmp/validate_audiospider_delivery.py
```

如果提示 `ffprobe_errors=[]`、`missing_sidecars=[]`、`json_parse_errors=[]`，说明物理文件和配套元信息通过了基础完整性检查。

## 七、如何处理失败记录

失败记录不会被删除。先按来源查看：

```bash
sqlite3 -readonly audiospider.db \
  "SELECT source, COUNT(*) FROM audio_urls WHERE status='failed' GROUP BY source;"
```

只有确认来源 URL 已恢复、域名可达且确实需要重试时，才做小批量重试：

```bash
python main.py --retry-failed --source podcast_rss --limit 20 --workers 1
```

不要在没有复核原因时直接执行全库 `--retry-failed`。Anchor/DNS 不可达、403、非音频响应和已经下线的历史 URL，通常需要重新发现或人工替换来源，而不是反复请求。

## 八、备份和迁移

数据库迁移前先停止下载进程，并使用 SQLite 在线备份或复制备份；不要在下载过程中覆盖 `audiospider.db`。音频目录可以通过 `rsync` 迁移：

```bash
rsync -a --info=progress2 downloads/ user@目标机器:/path/to/AudioSpider-fork/downloads/
```

迁移后重新运行 `python doctor.py`、`python main.py stats` 和全量验收脚本。数据库中的 `local_path` 是服务器路径，换机器后如果目录不同，需要先按项目文档处理路径映射，不要直接修改成不存在的路径。
