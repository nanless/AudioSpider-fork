# B 站视频与字幕数据集设计

## 目标

在不破坏现有 B 站纯音频 SQLite 队列的前提下，增加一条独立、可审计、可断点续传的视频采集管线。每个 BV 分 P 都形成自包含 bundle：合并视频、训练用 WAV、平台字幕原始 JSON、标准 VTT、纯文本以及完整 `metadata.json`。

## 为什么独立于旧下载器

旧管线的一条记录代表一个音频 URL，并把最终文件写入 `downloads/bilibili/`。B 站视频是 DASH 视频流和音频流两条 URL，需要合并；一个分 P 还可能有多条字幕轨。强行复用旧队列会改变既有 605 条音频记录的语义，也无法表达字幕轨道、视频流和 bundle 的完整性。因此新增 `bilibili_dataset.py`，旧 `collect.py/main.py` 行为保持不变。

## 数据布局

```text
downloads/bilibili-video-YYYYMMDD/
├── parents/<BV号>/p<分P>/<job_key>/
│   ├── source.mp4
│   ├── audio.wav
│   ├── captions.<语言>.<manual|automatic|unknown>.json
│   ├── captions.<语言>.<manual|automatic|unknown>.vtt
│   ├── captions.<语言>.<manual|automatic|unknown>.txt
│   └── metadata.json
├── manifests/
├── .locks/
└── .staging/
```

若没有平台字幕，仍可按清单策略保存视频，但 sidecar 必须显式记录 `caption.status=missing`；`--require-caption` 会让该项目失败关闭。默认不运行本地 ASR，因此平台字幕与项目生成文本不会混淆。

## 字幕来源判定

判定输出为三态，而不是强行二选一：

| kind | text_source | 含义 |
|---|---|---|
| `automatic` | `platform_auto` | 平台结构字段或明确标签证明为自动字幕 |
| `manual` | `platform_manual` | 轨道为普通 CC，且存在可核对的字幕作者 |
| `unknown` | `platform_unknown` | 字段冲突、缺失，或只能确认它来自平台 |

优先证据顺序：`type` > 明确作者信息 > `lan/lan_doc` 自动字幕标记。`ai_type` 在已观察接口中还可能表示“翻译”而非“字幕是否自动生成”，所以只原样保留，不能单独用于人工/自动判定。每次判定保存规则名、原始 `type/ai_type/ai_status` 和作者摘要。

字幕生成来源与媒体内容的 AI 来源分离：`caption.kind=automatic` 不会改变 `ai_generation.status`；后者没有可复核声明时保持 `unknown`。

## 下载和完整性

1. `inspect` 请求 view、pagelist/player/playurl，选定分 P、视频流、音频流和字幕轨。
2. 不把带签名 query 的 DASH/字幕 URL 写入正式 sidecar，只保存脱敏后的 host/path、轨道 ID 和响应字段。
3. 视频与音频分别下载到确定性的 `.staging/<job_key>`，支持 HTTP Range；完成后由 ffmpeg 合并为 MP4并抽取 16 kHz、单声道、PCM16 WAV。
4. 字幕 JSON 经严格解析后生成 VTT/TXT；无效时间、空文本、反向时间或超限 cue 会失败。
5. 写 sidecar 前计算字节数与 SHA-256，并用 ffprobe 验证 MP4 至少含视频、WAV 参数正确、时长合理。
6. 只有整个 bundle 校验通过才原子提升到 `parents/`；重复运行先验证现有 bundle，损坏目录不得当作成功复用。

## 安全和合规边界

- 只接受无凭据的 `https://www.bilibili.com/video/BV...` 或 BV 号。
- API host、媒体 CDN host 和字幕 host 使用 allowlist，并拒绝本机、私网、保留地址和重定向逃逸。
- 默认匿名公开访问；不绕过登录、会员、地域、验证码或访问控制。
- 对 manifest、响应体、字幕、单流、总 bundle、时长、分辨率、项目数和分 P 数设置硬上限。
- `copyright` 和公开可见性只作为来源元数据，不等于训练或再分发许可；默认 `rights.status=needs_review`。
- 每个任务使用文件锁，日志和错误都对签名 URL、Cookie、token 脱敏。

## CLI

```bash
python bilibili_dataset.py --output downloads/bilibili-video-20260910 \
  inspect --manifest config/bilibili_sources.example.json

python bilibili_dataset.py --output downloads/bilibili-video-20260910 \
  download --manifest config/bilibili_sources.example.json

python bilibili_dataset.py --output downloads/bilibili-video-20260910 audit
```

清单显式列 BV、分 P、字幕语言偏好、最大画质、是否强制字幕、权利和 AI/说话人数审核状态，禁止无限搜索即下载。
