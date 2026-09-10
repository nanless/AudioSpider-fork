# YouTube 完整视频、WAV 与同语言字幕指南

当前 YouTube 数据的**唯一正式路径**是：

```text
受控 manifest -> collect.py -> audiospider.db -> main.py
              -> downloads/youtube/<category>/<source_id>/<job_key>/
```

正式任务下载完整母视频，不自动切 clip。`youtube_dataset.py` 是兼容、修复和审计底层
工具，不是新批次的任务编排入口，也不能再把 `downloads/youtube-candidates/` 当作正式
数据根目录。

## 1. 一个正式父视频包含什么

有同语言平台字幕时：

```text
downloads/youtube/访谈/<video_id>/<job_key>/
├── source.mp4
├── audio.wav
├── captions.en.manual.vtt       # 或 automatic
├── captions.en.manual.txt
└── metadata.json
```

没有同语言字幕且 manifest 明确写 `require_caption=false` 时：

```text
downloads/youtube/访谈/<video_id>/<job_key>/
├── source.mp4
├── audio.wav
└── metadata.json                # caption.status=missing 或 no_matching_language
```

两种情况都必须是完整母视频和完整 WAV；无字幕样本不会生成空 VTT、空 TXT，也不会用
本地 ASR 结果冒充 YouTube 平台字幕。

## 2. 运行前检查

```bash
ssh dev_L4_1gpus
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider

python doctor.py
yt-dlp --version
ffmpeg -version | head -n 1
python main.py stats
df -h .
```

工具不登录 YouTube、不读取浏览器 Cookie，也不绕过 DRM、会员、付费、验证码、年龄或
地区限制。网络不可达时记录失败，不把失败伪装成“无字幕”。

正式采集前先验证服务器出口：

```bash
timeout 20 curl -I https://www.youtube.com
```

若返回 `Network is unreachable` 或超时，必须先取得用户批准的合规 HTTP(S) 代理或
任务期临时受限隧道。凭据只放当前进程环境，不写 `.env`、Git、SQLite、日志或 sidecar。

## 3. 编写受控 manifest

正式清单建议放在 `config/`。英文访谈示例：

```json
{
  "items": [
    {
      "url": "https://www.youtube.com/watch?v=VIDEO_ID",
      "profile": "youtube_interviews",
      "content_language": "en",
      "languages": ["en"],
      "require_caption": false,
      "max_parent_duration_seconds": 3636,
      "speaker_count": null,
      "speaker_count_status": "needs_review",
      "rights": {
        "status": "needs_review",
        "source": "public_video_page",
        "evidence_url": "https://www.youtube.com/watch?v=VIDEO_ID"
      },
      "ai_generation": {
        "status": "unknown",
        "evidence": []
      }
    }
  ]
}
```

字段要点：

- `url` 只接受无凭据的单视频 URL，不接受 playlist、直播或待开播视频。
- `profile=youtube_interviews` 会按 25.5–60.6 分钟长访谈策略核验完整母视频。
- `content_language=en` 表示内容主要语言为英文。
- `languages=["en"]` 只选择英文同族字幕，不用其他语言兜底。
- `require_caption=false` 表示同语言字幕 best effort；没有时仍保留媒体。
- 旧 manifest 未显式选择 best effort 时继续保持严格字幕语义。
- `speaker_count=null` 表示尚未人工核验，不允许从标题猜人数。
- `rights.status=needs_review` 表示公开可见但权利尚未清理。
- `ai_generation.status=unknown` 与字幕是否自动生成没有因果关系。

`require_caption`、语言策略、profile 和来源修订会进入不可变 `job_key` 身份。同一个视频
采用不同策略时不会互相覆盖。

## 4. 正式采集：只入 SQLite，不下载视频

```bash
AUDIOSPIDER_YOUTUBE_MANIFEST=config/youtube_interviews_50_20260910.json \
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=50 \
AUDIOSPIDER_YOUTUBE_INSPECT_TIMEOUT=120 \
python collect.py --spiders youtube
```

`collect.py` 会在可终止子进程中逐条核验：

- URL 是完整单视频且 video ID 稳定；
- 不是 live/upcoming；
- 时长符合 profile 和单项上限；
- 内容语言/字幕语言策略合法；
- 有英文人工或自动字幕时选择同语言轨；
- 没有英文字幕时，仅当 `require_caption=false` 才把无字幕事实入队。

检查队列：

```bash
python main.py stats
sqlite3 -readonly audiospider.db \
  "SELECT status,COUNT(*) FROM audio_urls WHERE source='youtube' AND artifact_kind='video_bundle' AND category='访谈' AND language='en' GROUP BY status;"
```

采集日志中的“清单 50 项”不等于“新增 50 项”：重复 `job_key` 不会重复插入，网络失败、
下架、直播或时长不合格项也不会静默算成功。

## 5. 正式下载：完整母视频，不生成 clip

第一次只下载一条：

```bash
python main.py --source youtube --artifact-kind video_bundle \
  --category 访谈 --language en --limit 1 --workers 1 --format original
```

通过抽查后再扩量：

```bash
python main.py --source youtube --artifact-kind video_bundle \
  --category 访谈 --language en --limit 10 --workers 2 --format original
```

`main.py` 在下载时重新解析可用格式，以最高 720p 等项目上限选择视频/音频并产出完整
MP4；随后生成 16 kHz mono PCM16 WAV。有字幕时同时获取选择的英文轨并派生 TXT。

这里不运行 `clip`。即使兼容工具还保留历史 clip 能力，也只有用户另行明确提出短片
需求时才能使用；短片不能代替或覆盖完整母视频。

## 6. 字幕 best effort 的准确含义

优先顺序是同语言人工轨，再到同语言自动轨：

| `caption.kind` | `caption.text_source` | 含义 |
|---|---|---|
| `manual` | `platform_manual` | YouTube 普通字幕轨；仍建议抽查文本质量 |
| `automatic` | `platform_auto` | YouTube 自动字幕，只能作为弱标签 |
| `unknown` | `platform_unknown` | 平台证据不足，不能擅自改成人工或自动 |

没有可接受英文轨时，best-effort sidecar 必须至少保存：

```json
{
  "caption": {
    "status": "missing",
    "kind": null,
    "text_source": null,
    "track_language": null,
    "requested_languages": ["en"],
    "required": false
  }
}
```

具体字段可能随 schema 版本增加，但以下语义不能改变：

- `missing` 表示平台没有可用字幕轨；`no_matching_language` 表示存在字幕轨、但没有英文同族轨；
- 网络/解析失败必须作为下载失败或外部错误，不能降级成 `missing`；
- 不允许创建假的 VTT/TXT 以满足文件数量；
- 如果未来另跑本地 ASR，必须用独立 provenance 和独立产物名，不能写成 `platform_*`；
- 自动字幕不证明视频内容由 AI 生成。

## 7. 断点续跑与超时

YouTube 核验和下载分别在可终止子进程内运行。常用硬超时：

| 变量 | 默认 | 含义 |
|---|---:|---|
| `AUDIOSPIDER_YOUTUBE_INSPECT_TIMEOUT` | `120` 秒 | 单视频元数据/字幕核验 |
| `AUDIOSPIDER_YOUTUBE_DOWNLOAD_TIMEOUT` | `14400` 秒 | 单完整 bundle 下载 |

中断后：

- SQLite lease 到期的 `downloading` 会按统一机制回收；
- 完整 bundle 已提交为 `done` 时不会重复下载；
- 未完成 staging 不会被计入正式完成数；
- 已保存的恢复描述不含签名 URL；
- 只对已分类的临时失败做有界重试。

```bash
python main.py --retry-failed --source youtube \
  --category 访谈 --language en --artifact-kind video_bundle \
  --limit 5 --workers 1 --format original
```

## 8. 统一审计与抽查

```bash
python scripts/audit_media_queue.py
```

该命令统一验证 YouTube 与 B站所有 `done video_bundle`：数据库/目录身份、MP4/WAV、
字幕闭包、sidecar、字节数、SHA-256、content hash 和孤儿目录。

抽查一个父视频：

```bash
ffprobe -v error -show_streams -show_format \
  downloads/youtube/访谈/<video_id>/<job_key>/source.mp4
ffprobe -v error -show_streams -show_format \
  downloads/youtube/访谈/<video_id>/<job_key>/audio.wav
python -m json.tool \
  downloads/youtube/访谈/<video_id>/<job_key>/metadata.json
```

重点确认：视频时长是完整父视频、MP4 同时有视频与音频流、WAV 是 16 kHz mono PCM16、
字幕语言为英文、`kind/text_source` 配对正确、无字幕样本没有伪造字幕文件。

## 9. 权利、说话人数与 AI 状态

- 公开视频不等于允许批量下载、训练、公开数据集或商业再分发。
- `rights.status=needs_review` 不是已获权；必须保留并由数据负责人/法务审核。
- 未人工核验时 `speaker_count=null`、`speaker_count_status=needs_review`。
- `caption.kind=automatic` 只描述文本轨，不设置媒体 `ai_generation.status`。
- 听感或模型输出最多支持 `suspected`，不能冒充来源声明。

## 10. standalone CLI 什么时候还能用

`youtube_dataset.py` 只用于：

- 修复旧父 bundle 的可确定派生字段；
- 审计或迁移历史 standalone 数据；
- 为统一 handler 提供底层验证函数；
- 用户明确要求的隔离兼容实验。

新任务必须由 `collect.py` 入 `audiospider.db`，再由 `main.py` 下载到
`downloads/youtube/...`。旧目录使用 `scripts/migrate_video_bundles.py` 默认 dry-run，
确认后才 `--apply`，迁移结束再跑统一审计。

本次 100+50 访谈批次的完整步骤见
[150 个中英文完整访谈批次运行手册](interview-batches-150.md)。
