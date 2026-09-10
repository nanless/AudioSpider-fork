# B 站视频、WAV 和平台字幕小白指南

这条管线适合“我要保留视频，并尽量同时保存 B 站平台字幕”的任务。它与旧的 B 站纯音频下载是两套互不覆盖的流程：

- `collect.py` + `main.py`：把 B 站分 P 当成音频任务，结果在 `downloads/bilibili/`。
- `bilibili_dataset.py`：把每个分 P 做成视频数据 bundle，结果建议放在 `downloads/bilibili-video-日期/`。

不要把两套目录混在一起，也不要用旧 SQLite 的临时音频 URL 作为视频来源。

## 1. 每个样本里有什么

有平台字幕时：

```text
source.mp4                                      合并后的音视频
audio.wav                                       16 kHz、单声道、PCM16
captions.zh-Hans.manual.123.json                平台原始字幕 JSON
captions.zh-Hans.manual.123.vtt                 标准 WebVTT
captions.zh-Hans.manual.123.txt                 纯文本
metadata.json                                   完整背景信息和哈希
```

同一个分 P 如果公开了多条字幕，会全部保存；文件名里的语言、kind 和字幕 ID 可防止覆盖。

没有可取字幕时仍会保存 MP4、WAV 和 sidecar，但 `metadata.json` 会写清原因：

- `not_provided_publicly`：公开接口成功并明确返回空轨道。
- `auth_required`：平台说明需要登录后才能判断或取得字幕。
- `unknown`：响应字段不足，不能下结论。

设置清单中的 `require_caption=true` 后，以上三种情况都会拒绝完成样本。

## 2. 第一次运行

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
```

先复制示例清单：

```bash
cp config/bilibili_sources.example.json config/bilibili_sources.local.json
```

编辑 `items`，一个对象代表一个 BV。最重要的字段：

```json
{
  "bvid": "BV1xxxxxxxxx",
  "parts": [1],
  "max_parts": 1,
  "max_height": 480,
  "max_duration_seconds": 1800,
  "languages": ["zh-Hans", "zh-CN", "ai-zh"],
  "require_caption": false,
  "content_language": "zh",
  "rights": {"status": "needs_review"},
  "ai_generation": {"status": "unknown", "evidence": []}
}
```

含义：

- `parts`：只下载指定分 P；空数组表示从前往后取，但仍受 `max_parts` 限制。
- `max_height`：最高分辨率，范围 144–1080；初次推荐 480 或 720。
- `languages`：字幕语言优先范围，必须与 `content_language` 一致。英文内容只允许英文字幕；中文/粤语内容只允许中文或粤语字幕。空数组会按内容语言自动生成本语言候选，不再表示“所有语言”。
- `require_caption`：是否把“没有可下载字幕”当失败。
- `rights`：使用权审核；公开可看不能自动改成 cleared。
- `ai_generation`：视频内容是否 AI 生成；不能用自动字幕反推。

只检查、不下载：

```bash
env -u BILIBILI_COOKIE python bilibili_dataset.py \
  --output downloads/bilibili-video-candidates \
  inspect --manifest config/bilibili_sources.local.json
```

确认 BV、CID、分 P、时长和字幕状态后再下载：

```bash
env -u BILIBILI_COOKIE python bilibili_dataset.py \
  --output downloads/bilibili-video-candidates \
  download --manifest config/bilibili_sources.local.json
```

最后审计：

```bash
python bilibili_dataset.py \
  --output downloads/bilibili-video-candidates audit \
  --manifest config/bilibili_sources.local.json
```

只有 `failure_count=0`、`incomplete_staging_count=0` 才说明技术闭包通过。

## 3. 字幕是怎么判人工还是自动

每条轨道独立判定：

| kind | text_source | 可说的结论 |
|---|---|---|
| `manual` | `platform_manual` | 平台把它作为普通 CC 轨道，且有可核对作者；不等于逐字人工验真 |
| `automatic` | `platform_auto` | 平台结构字段或明确标签证明它来自智能/自动字幕通道 |
| `unknown` | `platform_unknown` | 字段缺失、未来枚举或证据冲突 |

`type` 是主判定信号；`ai_type` 单独记录为翻译维度：

- `normal`
- `automatic_translation`
- `unknown`

自动字幕可能被 UP 主修改，但生成来源仍不会升级成人工。反过来，UP 主把外部 AI 文本当普通 CC 上传时，平台接口也无法证明制作过程；sidecar 只陈述平台可见事实。

## 4. 登录后才可见的字幕

匿名接口经常返回 `need_login_subtitle=true`。默认程序不会读取浏览器 Cookie，也不会绕过登录或会员限制。

如果你对该内容有访问权，可以在当前 shell 临时注入 B 站 Cookie：

```bash
read -r -s BILIBILI_COOKIE
export BILIBILI_COOKIE
python bilibili_dataset.py --output downloads/bilibili-video-candidates \
  inspect --manifest config/bilibili_sources.local.json
unset BILIBILI_COOKIE
```

Cookie 只进入 HTTP 请求头，不写 manifest、日志、sidecar 或下载目录。不要把 Cookie 写进 JSON、Git、命令历史或聊天消息。登录也不保证每条视频都有字幕。

`read -s` 会让你在服务器终端无回显地输入 Cookie，命令历史只记录 `read`，不记录实际值。普通匿名任务显式使用 `env -u BILIBILI_COOKIE`，避免误用 shell 中遗留的登录态。

升级旧版 sidecar 的派生字段时可运行：

```bash
python bilibili_dataset.py --output downloads/bilibili-video-candidates repair-metadata
```

它只重建 probe/声明时长等派生元数据，不替换原视频和原字幕。

## 5. 下载和断点续传是怎么工作的

程序不依赖 B 站网页解析。它先从公开 view API 获取分 P 与 CID，再从 playurl API选择不超过清单上限的 DASH 视频和音频表示层：同分辨率优先 AVC，然后按带宽选择；音频选最高带宽。

视频流和音频流分别下载到 `.staging/<job_key>`。状态文件只保存 codec、清晰度、带宽等表示层指纹，不保存签名 URL。重试时只有指纹相同且服务端返回精确 `Content-Range` 才会追加；否则重新下载，HTTP 416 不会被误当成完成。

ffmpeg 使用两个明确输入合并：第一个输入的视频流和第二个输入的音频流。先尝试无损 remux，容器不兼容时才转 H.264/AAC，并把方式写入 `source_streams.merge_mode`。随后抽取训练用 WAV。

## 6. 为什么目录里有 `.audiospider-dataset.json`

这是保护标记。旧的 `convert_audio.py` 默认递归扫描 `downloads/` 并把 WAV 转 Opus；它现在看到该标记会跳过整个视频数据集，避免删除 `audio.wav` 后破坏 sidecar/hash。B 站视频的正式位置就是 `downloads/bilibili-video-日期/`。

## 7. 常见问题

### 视频有画面字幕，为什么没有平台字幕文件

画面里“烧录”的字幕是视频像素，不一定存在独立字幕轨。程序只下载 B 站 player API 真正公开的字幕，不用 OCR 猜文字。

### `auth_required` 是失败吗

在 `require_caption=false` 时不是媒体下载失败；它是重要的数据事实。需要字幕的训练集应该筛掉它，或在合法登录环境下重新 inspect/download。

### 自动字幕是不是说明视频声音是 AI 生成

不是。`caption.kind` 只描述文本轨；媒体内容使用独立的 `ai_generation`，没有平台声明或人工证据时保持 `unknown`。

### 能不能直接全部下载一个几百 P 的合集

技术上 `parts=[]` 可从头取，但 `max_parts` 必须是有限正数。先检查时长、磁盘和许可，再按小批清单扩量。不要移除上限。

### 数据下载到哪里

正式首批默认约定为：

```text
/root/code/github_repos/AudioSpider-fork/downloads/bilibili-video-20260910
```

父样本在 `parents/<BV>/p<分P>/<job_key>/`，每轮 inspect/download 记录在 `manifests/`。
