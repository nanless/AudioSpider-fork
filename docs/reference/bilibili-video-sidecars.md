# B 站视频 bundle 与 sidecar 参考

## 目录契约

正式父 bundle 只能位于：

```text
parents/<bvid>/p<page>/<job_key>/
```

`job_key` 绑定 BVID、CID、分 P、清晰度、字幕语言、`require_caption`、`source_revision`、schema 和编码 profile。分 P 顺序可能变化，CID 是更稳定的具体媒体身份。

`metadata.json` 是完成标记，但程序不会仅凭文件存在就复用：重跑会重新检查 schema、文件闭包、字节数、SHA-256、ffprobe 和字幕派生一致性。

## 顶层字段

| 字段 | 含义 |
|---|---|
| `schema_version` | 当前为 1 |
| `asset_type` | 固定 `bilibili_parent` |
| `source/source_id` | `bilibili` 与旧音频兼容 ID `BV..._pN` |
| `bvid/aid/cid/part` | 视频、稿件、分 P 媒体和当时页序 |
| `job_key` | bundle 幂等键 |
| `canonical_url` | 不含凭据的 BV 分 P 页面 |
| `source_metadata` | 标题、简介、封面、UP 主、分类、统计、平台 rights/copyright |
| `source_streams` | 无签名 URL 的 DASH 表示层描述及合并方式 |
| `acquisition_policy` | 本轮画质、时长、字幕和修订策略 |
| `media/audio` | ffprobe 得到的实际流、codec、分辨率、采样率和时长 |
| `caption` | 字幕可用性和逐轨证据 |
| `files` | 所有 payload 的相对路径、字节数和 SHA-256 |
| `rights` | 项目侧权利审核，不由平台 `copyright` 自动推导 |
| `ai_generation` | 媒体内容 AI 来源，与字幕 kind 独立 |
| `speaker_count` | 人工核验前为 null |
| `toolchain` | 编码 profile |

## `caption.status`

| 值 | 条件 | 正式 bundle 是否允许 |
|---|---|---|
| `downloaded` | 至少一条字幕 JSON/VTT/TXT 已闭合 | 允许 |
| `not_provided_publicly` | 成功响应、明确无需登录、轨道为空 | `require_caption=false` 时允许 |
| `auth_required` | 成功响应且 `need_login_subtitle=true`、轨道为空 | `require_caption=false` 时允许 |
| `no_matching_language` | 平台有轨道，但没有清单允许的语言 | `require_caption=false` 时允许 |
| `unknown` | 响应成功但字段不足 | `require_caption=false` 时允许并报警统计 |

HTTP/API 失败不会被写成上述“空字幕”状态，也不会提升正式 bundle。

## 单条 caption track

```json
{
  "id": 123,
  "id_str": "123",
  "url_redacted": "https://aisubtitle.hdslb.com/path/file.json",
  "track_type": 1,
  "language": "ai-zh",
  "label": "中文（自动生成）",
  "ai_type": 0,
  "ai_status": 2,
  "kind": "automatic",
  "text_source": "platform_auto",
  "translation_kind": "normal",
  "selected_by_rule": "bilibili_type_ai",
  "rule_version": "bilibili-caption-provenance-v1",
  "cue_count": 321,
  "files": {
    "json": "caption_0_json",
    "vtt": "caption_0_vtt",
    "txt": "caption_0_txt"
  }
}
```

`url_redacted` 只有 HTTPS host/path，无 query、token 或签名。真实下载 URL 只在内存中存在。

## 文件闭包

无字幕 bundle 精确包含 `source.mp4`、`audio.wav`、`metadata.json`。

每条已下载字幕增加三个文件：

- 平台原始 JSON：可重建的事实源。
- VTT：毫秒时间戳，供播放器和切片工具使用。
- TXT：按 cue 顺序展开的清洗文本。

审计会重新解析 JSON 并重建 VTT/TXT 后比较，任一字幕正文被改动、文件遗漏、路径逃逸、软链接、字节数或哈希不符都会失败。

## 审计硬条件

- 数据集至少有一个正式 sidecar。
- `.staging` 不能残留目录。
- MP4 必须同时有视频流和音频流，实际高度不能超过清单策略。
- WAV 必须是单个 PCM16、16 kHz、单声道音频流。
- MP4/WAV 时长差不能超过 0.5 秒。
- `kind` 与 `text_source` 必须一致。
- 所有普通 payload 文件必须恰好出现在 `files` 中，不允许未记录文件。

运行：

```bash
python bilibili_dataset.py --output datasets/bilibili-video-20260910 \
  audit --manifest config/bilibili_sources.initial.json
```
