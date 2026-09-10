# YouTube 完整视频与同语言平台字幕小白指南

本页面向第一次接触命令行和数据集的人，讲清楚如何得到以下两类数据：

- 长访谈/播客：保留整段视频、整段 WAV、平台字幕、纯文本和 JSON；
- 影视/节目视频：保留完整父视频、完整 WAV、同语言平台字幕、文本和 JSON，不生成短 clip。

## 1. 开始前必须知道的四件事

第一，`公开可播放` 不等于 `可以再分发或用于训练`。清单中的 `rights.status=needs_review` 表示只是候选样本。是否允许训练、公开数据集或商业使用，需要根据许可证、授权和当地法律另行判断。

第二，本工具不登录 YouTube，不使用 Cookie，不绕过 DRM、会员、付费、验证码、年龄或地区限制。访问不到就记录失败，不尝试突破平台控制。

第三，平台字幕有两种：

| sidecar 值 | 含义 | 建议用途 |
|---|---|---|
| `platform_manual` | 上传者或平台提供的人工字幕轨 | 可以作为较高质量参考，仍需抽查 |
| `platform_auto` | YouTube 自动语音识别字幕 | 只能当弱标签，训练前建议清洗 |

第四，说话人数不会从标题猜。没有人工核验时一定保存为 `speaker_count=null` 和 `speaker_count_status=needs_review`。

第五，是否 AI 生成也不能只凭听感断言。没有平台/作者声明或可复核检测证据时保存 `ai_generation.status=unknown`；详细取值见 [YouTube sidecar 参考](../reference/youtube-sidecars.md)。

## 2. 环境准备

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python --version
yt-dlp --version
ffmpeg -version | head -n 1
```

如果 `yt-dlp` 不存在，更新环境：

```bash
bash scripts/bootstrap_conda.sh
```

当前锁定版本见 `requirements-lock.txt`。不要在系统 Python 中随意安装依赖。

中国大陆网络通常不能直连 YouTube。若组织已经提供合规代理，可在当前终端临时设置 `HTTPS_PROXY`/`HTTP_PROXY`；不要把带用户名、密码或 token 的代理地址写进仓库、清单、sidecar 或日志。

## 3. 复制并填写来源清单

先复制示例：

```bash
cp config/youtube_sources.example.json /tmp/my-youtube-sources.json
```

每个 `items` 元素代表一个公开视频：

```json
{
  "url": "https://www.youtube.com/watch?v=视频ID",
  "profile": "youtube_screen_clips",
  "content_language": "yue",
  "languages": ["yue", "zh-Hant", "zh-HK", "zh"],
  "speaker_count": null,
  "speaker_count_status": "needs_review",
  "program": "节目或影视名称",
  "rights": {
    "status": "needs_review",
    "source": "官方频道公开视频",
    "evidence_url": "规范视频页面"
  }
}
```

字段解释：

- `url`：只接受单视频 `https://youtube.com/watch?v=...` 或 `https://youtu.be/...`，不接受播放列表和任意网站。
- `profile`：长访谈用 `youtube_interviews`；影视/节目父视频沿用兼容名称 `youtube_screen_clips`，但下载动作只产生完整父视频。
- `content_language`：音视频对白的主要语言；粤语写 `yue`，简体中文可写 `zh-Hans`。
- `languages`：字幕选择优先顺序，必须与对白语言一致。英文内容只能列英文；中文/粤语内容只能列中文或粤语；日语、韩语同样只列本语言。
- `speaker_count`：只有人工看过并确认时才填整数。
- `speaker_count_status`：有整数时必须是 `verified_manual`；未知必须是 `needs_review`。
- `rights`：来源与权利证据。`needs_review` 不会被误写成已经获权。

长访谈的人工说话人数必须在 2–11；影视目标片段必须在 1–6。未核验样本可以作为候选下载，但不能算作已通过人数验收的数据。

## 4. 第一步只探测，不下载

```bash
python youtube_dataset.py \
  --output downloads/youtube-candidates \
  inspect \
  --manifest /tmp/my-youtube-sources.json
```

命令会逐条读取标题、频道、时长和字幕轨，输出一次独立的 JSONL：

```text
downloads/youtube-candidates/manifests/inspect-时间-runid.jsonl
```

重点检查：

- `status=accepted`；
- 长访谈 `duration_seconds` 在 1530–3636 秒；
- `caption.kind` 是 `manual` 或 `automatic`；
- `caption.text_source` 与 kind 一致；
- `content_language` 没有被英文字幕错误覆盖；
- `rights_cleared=false` 的候选没有被当作已授权数据。

没有可接受字幕、直播、待开播、时长不合法或非单视频 URL 会直接失败。

## 5. 下载父视频、音频和字幕

```bash
python youtube_dataset.py \
  --output downloads/youtube-candidates \
  download \
  --manifest /tmp/my-youtube-sources.json
```

只下载清单中的一条可追加 `--video-id`；多条时重复参数：

```bash
python youtube_dataset.py --output downloads/youtube-candidates download \
  --manifest /tmp/my-youtube-sources.json \
  --video-id b2f2Kqt_KcE --video-id KAKkwvZ96eU
```

工具限制为单视频、最高 720p、最大 8 GiB，不读浏览器 Cookie。每条数据先在 `.staging` 目录生成；视频、WAV、字幕、文本和 metadata 全部完成后才整体移动到最终目录。

长访谈目录示例：

```text
downloads/youtube-candidates/interviews/VIDEO_ID/en/JOB_KEY/
├── source.mp4
├── audio.wav
├── captions.en.manual.vtt
├── captions.en.manual.txt
└── metadata.json
```

影视父视频目录位于 `screen_sources/`，结构相同。

`audio.wav` 固定为 16 kHz、单声道、PCM16，适合大多数 ASR/VAD/说话人任务。`source.mp4` 保留画面和音轨，必要时会由 ffmpeg 规范为 MP4。

重跑同一个清单时，如果最终 `metadata.json` 已存在，会复用已完成 bundle，不重复下载。未完成的确定性 staging 会保留 `.part` 和脱敏 `failure.json` 以便续传；每次运行的结果写入新的 `download-*.jsonl`，不会覆盖旧运行记录。

## 6. 当前正式流程不生成 clip

不要为当前视频数据集运行 `clip`。命令行现在默认拒绝 clip，只有用户明确提出短片需求时，才可以额外提供 `--allow-clips`：

```bash
PARENT='downloads/youtube-candidates/screen_sources/VIDEO_ID/字幕语言/JOB_KEY'
python youtube_dataset.py \
  --output downloads/youtube-candidates \
  clip --parent "$PARENT" --max-clips 100 --allow-clips
```

这只是向后兼容的底层能力，不属于本项目当前下载流程。历史生成的 clips 已从正式数据集移到 `archive/youtube-clips-20260910/`，未删除，可恢复。

## 7. 全量审计

```bash
python youtube_dataset.py --output downloads/youtube-candidates audit
```

审计会：

- 对每个 MP4/WAV 调用 ffprobe；
- 对每个文件重算 SHA-256；
- 验证 sidecar 内相对路径没有越界；
- 验证 VTT 至少有一个有效 cue；
- 平台原始字幕若仅在结尾轻微超出媒体，会作为 warning 保留原件；派生片段会严格裁到父视频实际结尾；
- 检查 sidecar 是否意外保存 `token=`、`sig=`、`expire=` 等临时签名；
- 汇总 profile、语言、人工/自动字幕和说话人数核验状态；
- 正式父视频与字幕语言不一致时直接失败。

`failure_count=0` 才表示文件闭包通过。它不代表权利已经确认，也不代表自动字幕文字百分之百正确。

旧 bundle 若因字幕规范化规则升级出现“VTT 与 TXT 不一致”，可从保留的原始 VTT 安全重建派生文本：

```bash
python youtube_dataset.py repair-parent --parent "$PARENT"
```

该命令会先核验父视频、WAV 和 VTT 的既有字节数与 SHA-256，只重建 TXT、媒体探针字段和对应 sidecar；源视频和原始平台 VTT 不会被改写。

整批 schema/provenance 升级使用：

```bash
python youtube_dataset.py --output downloads/youtube-candidates repair-dataset
```

它会修复全部父 bundle 的派生字段；媒体与原始 VTT 仍保持不变。

## 8. 怎么判断自动字幕

不要看文件名猜，直接查 JSON：

```bash
python - <<'PY'
import json
from pathlib import Path

for path in Path('downloads/youtube-candidates').glob('**/metadata.json'):
    data = json.loads(path.read_text(encoding='utf-8'))
    caption = data.get('caption') or {}
    print(path, caption.get('kind'), caption.get('text_source'))
PY
```

以下组合是合法的：

- `manual + platform_manual`；
- `automatic + platform_auto`。

如果出现交叉组合，说明数据损坏或 sidecar 被手工改错，应从正式数据集中隔离。

## 9. 常见问题

### 为什么下载很慢

长访谈包含 25–60 分钟视频，还要额外解出整段 WAV。代理出口、YouTube 分片速度、ffmpeg 和磁盘都可能成为瓶颈。先用 1–3 个视频验证，不要直接给数百个 URL。

### 为什么只拿到英文字幕

现在不会这样做。英文内容只选英文字幕，中文/粤语内容只选中文或粤语字幕。平台只有英文字幕而中文视频没有中文字幕时，该视频会被拒绝或从正式集隔离，不能用英文字幕兜底。

### 为什么某个视频被拒绝

查看最新 `manifests/inspect-*.jsonl` 或 `download-*.jsonl`。常见原因是没有平台字幕、长访谈时长越界、直播、视频下架、地区不可用、代理失败或 YouTube 页面变化。

### 可以把 `needs_review` 数据公开吗

不能根据这个工具的结果得出可以公开的结论。`needs_review` 就是尚未确认。应让数据负责人/法务检查许可证、授权范围、训练用途和再分发条款，确认后再更新权利状态与证据。

## 10. 推荐的批次节奏

1. 清单先放 3 个长访谈、每种目标语言 1 个影视来源。
2. 全部 inspect，删除无字幕或时长不合格项。
3. download 1 个长访谈和 2 个影视父视频，不运行 `clip`。
4. audit 为 0 后，人工抽看字幕语言和 A/V 同步。
5. 再逐步增加到 10、50、100 个来源，并记录每批下载字节和失败率。

详细字段定义见 [YouTube 数据集 sidecar](../reference/youtube-sidecars.md)，整体数据流见[数据流与状态机](../design/data-flow.md)。
