# 2026-09-10 YouTube 长访谈与影视短片数据验收报告

## 1. 结论

在 `dev_L4_1gpus` 完成了 YouTube 独立数据流水线的开发、真实下载、字幕切片、缺陷修复和全量审计。

最终候选数据目录：

```text
/root/code/github_repos/AudioSpider-fork/downloads/youtube-staging-20260909
```

最终结果：

- 4 个长访谈父视频；
- 5 个多语言影视/节目父视频；
- 101 个字幕对齐 v2 短片；
- 110 个完整 bundle；
- 每个 bundle 都有 MP4、16 kHz 单声道 PCM16 WAV、VTT、TXT 和 `metadata.json`；
- 合计 550 个 bundle 文件，1,745,397,867 bytes；
- 严格 audit：`failure_count=0`；
- 未完成 staging：0；
- 自动/人工字幕、内容语言、字幕语言、AI 生成状态、说话人数状态和权利状态均有机器字段。

这些数据的 `rights.status=needs_review`、`rights_cleared=false`，因此是隔离候选集，不应宣称已经获得训练、公开发布或再分发授权。所有样本的说话人数目前也仍是 `needs_review`，不能把目标范围当作已经人工核验的标签。

## 2. 运行环境

| 项目 | 实测值 |
|---|---|
| 主机 | `dev_L4_1gpus` |
| 仓库 | `/root/code/github_repos/AudioSpider-fork` |
| 隔离开发 worktree | `/tmp/audiospider-youtube-worktree` |
| 开发分支 | `codex/youtube-media` |
| Conda 环境 | `/root/miniforge3/envs/audiospider` |
| Python | 3.11.16 |
| ffmpeg | 4.2.7 |
| yt-dlp | 2026.08.19 |
| 数据盘 | 130 TB 级挂载，任务启动前约剩 32 TB |

服务器可 SSH 且可访问国内 PyPI，但不能直连 YouTube：系统 DNS 曾把 `www.youtube.com` 解析到错误地址，HTTPS 超时。真实探测和下载通过 SSH 反向端口临时使用本机已有合规代理；没有修改服务器全局代理、没有写入代理凭据、没有使用 YouTube 登录或 Cookie。

## 3. 新增能力

### 3.1 两个 profile

`youtube_interviews`：

- 父视频时长必须 1530–3636 秒，即 25.5–60.6 分钟；
- 保存整段 MP4、整段 WAV、平台 VTT、规范化 TXT 和 sidecar；
- 目标说话人数 2–11，但只有人工核验后才可写 `verified_manual`。

`youtube_screen_clips`：

- 先保存影视/节目父视频；
- 按平台字幕 cue 生成短片；
- 实际 MP4 和 WAV 都必须处于 0.418–29.888 秒；
- 批次目标均值约 11.5 秒；
- 每个短片保留父视频/字幕哈希、起止毫秒和算法版本。

### 3.2 字幕 provenance

字幕选择遵循请求语言优先、人工优先、同语族回退的确定性规则。最终只允许：

- `manual + platform_manual`；
- `automatic + platform_auto`。

平台未明确翻译关系时，保存 `is_translated=null`、`translation_kind=unknown`，不再把未知误写为“明确未翻译”。内容语言与字幕语言分开，例如 TVB 样本是粤语对白、英文人工字幕。

### 3.3 数据安全和恢复

- 只接受无凭据的 HTTPS YouTube 单视频 URL；
- `noplaylist=True`，拒绝直播和待开播；
- 所有视频分支都限制到最高 720p；
- 单流 8 GiB、完整 bundle 10 GiB、字幕 50 MiB、父视频 4 小时上限；
- 不使用账号、Cookie、DRM/付费/验证码/地区控制绕过；
- `.staging/<job_key>` 确定性续传；
- 相同 job 使用排他文件锁；
- ffmpeg 和文本输出先写唯一临时文件，fsync 后原子替换；
- 完整 closure 校验后才把父目录提升为完成状态；
- sidecar 不保存 yt-dlp 格式字典或签名媒体/字幕 URL。

## 4. 真实父视频

### 4.1 长访谈

| video ID | 标题 | 时长 | 字幕 |
|---|---|---:|---|
| `u7TwqpWiY5s` | Conan O'Brien \| Talks at Google | 2904 秒 / 48.40 分钟 | en 人工 |
| `azzJur0cukc` | Jeff Probst \| Talks at Google | 3146 秒 / 52.43 分钟 | en 自动 |
| `guZa7mQV1l0` | Never Split the Difference \| Chris Voss | 3043 秒 / 50.72 分钟 | en 自动 |
| `Ye85L2r0hWw` | Neil Gorsuch \| Firing Line / PBS | 1626 秒 / 27.10 分钟 | en-US 人工 |

长访谈统计：

- 数量：4；
- 最短：27.10 分钟；
- 最长：52.43 分钟；
- 平均：2679.75 秒，即 44.6625 分钟；
- 全部位于 25.5–60.6 分钟硬范围；
- 人工字幕 2 条，自动字幕 2 条。

### 4.2 影视/节目父视频

| video ID | 来源 | 内容语言 | 字幕语言/类型 | 时长 |
|---|---|---|---|---:|
| `b2f2Kqt_KcE` | Movieclips | en | en 人工 | 143 秒 |
| `q3iimGb-lPc` | KBS WORLD TV | ko | ko 自动 | 78 秒 |
| `hNc-8ZGFJWU` | TVB (official) | yue | en 人工 | 714 秒 |
| `0p93BargoME` | YOUKU English | zh-Hans | en 人工 | 576 秒 |
| `KAKkwvZ96eU` | Netflix Anime | ja | ja 人工 | 57 秒 |

父视频共 9 条：人工字幕 6、自动字幕 3。实际来源清单为 `config/youtube_sources.initial.json`。

## 5. v2 影视短片

| 内容语言 | 短片数 |
|---|---:|
| 英语 `en` | 7 |
| 日语 `ja` | 3 |
| 韩语 `ko` | 6 |
| 粤语 `yue` | 50 |
| 简体中文 `zh-Hans` | 35 |
| 合计 | 101 |

时长统计：

- 最短：0.800 秒；
- 平均：11.605277 秒；
- 最长：16.100 秒；
- 低于 0.418 秒：0；
- 超过 29.888 秒：0。

101 条短片中，95 条继承人工平台字幕，6 条继承韩语自动平台字幕。父视频 9 条与短片合并后，严格审计统计为 `manual=101`、`automatic=9`。

## 6. 实际发现并修复的问题

### 6.1 yt-dlp 组合流被 `max_downloads=1` 误伤

首轮短视频下载完成视频流和音频流后，yt-dlp 抛出 `Maximum number of downloads reached`，staging 被当作失败。根因是该参数不适合一个视频的组合格式下载。

修复：移除 `max_downloads`，继续用单视频 URL 校验、`noplaylist=True` 和返回 video ID 复核保证单视频边界，并增加回归测试。

### 6.2 韩语自动字幕尾部超过媒体

KBS 父视频实测 77.927 秒，平台原始自动字幕最后 cue 到 79.540 秒，超出 1.613 秒。v1 因按字幕结束时间切片，最后一个目标 3.010 秒的片段实际只能生成约 1.420 秒媒体。

修复：保留原始平台 VTT 并记录 warning；切片前按 ffprobe 实测父媒体时长裁剪 cue。v2 韩语生成 6 条合法短片，不再包含坏片。

### 6.3 短片平均时长偏短

v1 使用 2 秒字幕间隔阈值，117 条短片平均约 9.31 秒。真实多语言 VTT 扫描表明 5 秒阈值的批次均值最接近目标。

修复：算法升级到 `subtitle-group-v2`，间隔阈值改为 5 秒，最终 101 条平均 11.605 秒。

### 6.4 人工重复台词被错误去重

旧解析会把上一 cue 出现过的整行从下一 cue 删除，人工字幕连续两句 `No` 时第二句会丢失。

修复：人工字幕不做跨 cue 消重；自动字幕只删除“上一 cue 后缀等于当前 cue 前缀”的确定性滚动重叠。已有父 bundle 通过 `repair-parent` 从原始 VTT 重建 TXT 和哈希。

### 6.5 审计过于宽松

代码审查复现了空目录、空 `{}` sidecar、未完成 staging 仍可能退出 0。

修复后的 audit 会拒绝：

- 空数据集、空/错 schema sidecar；
- 缺失或额外 bundle 文件；
- 路径逃逸和 symlink；
- bytes/SHA-256 不一致；
- 无音视频流、超过 720p；
- WAV 非 PCM16/16 kHz/单声道；
- VTT 无头、无有效 cue、非单调或超出短片；
- TXT 与按字幕类型规范化后的 VTT 不一致；
- MP4/WAV 实测时长越界或不同步；
- 父子 ID/job/hash 不闭合；
- 人工/自动字幕 provenance 交叉；
- 权利、AI 生成和说话人数状态不合法；
- 任何未完成 staging。

## 7. 最终严格审计

最终命令：

```bash
cd /tmp/audiospider-youtube-worktree
/root/miniforge3/envs/audiospider/bin/python youtube_dataset.py \
  --output /root/code/github_repos/AudioSpider-fork/downloads/youtube-staging-20260909 \
  audit
```

关键输出：

```text
asset_type:youtube_parent      9
asset_type:youtube_screen_clip 101
caption_kind:manual            101
caption_kind:automatic         9
ai_generation:unknown          110
rights:candidate               110
speaker_status:needs_review    110
failure_count                  0
incomplete_staging_count       0
```

唯一 warning 是 KBS 原始平台自动字幕尾部超过父媒体 1.613 秒。原始 VTT 为 provenance 证据所以保留；所有派生短片已裁到父媒体结尾并通过硬校验。

## 8. 文件放在哪里

```text
/root/code/github_repos/AudioSpider-fork/downloads/youtube-staging-20260909/
├── interviews/       # 4 个长访谈父 bundle
├── screen_sources/   # 5 个影视/节目父 bundle
├── screen_clips/     # 101 个 v2 短片 bundle
├── manifests/        # 每次 inspect/download 的独立 JSONL
├── .locks/           # job 文件锁
└── .staging/         # 当前为空
```

v1 旧短片没有删除，已可恢复地移到：

```text
/root/code/github_repos/AudioSpider-fork/downloads/youtube-staging-20260909-screen-clips-v1-archive
```

旧目录约 137 MiB，不属于最终 v2 audit 范围。

## 9. 每个样本的 sidecar

所有 110 个样本都包含：

- YouTube video/channel ID、规范 watch URL、标题、简介、上传日期和获取时间；
- profile、内容语言、请求字幕语言、实际字幕语言；
- `manual/platform_manual` 或 `automatic/platform_auto`；
- 翻译是否已知、选择规则、字幕尾部越界；
- 说话人数值与人工核验状态；
- AI 生成状态与证据数组；
- 权利状态、证据、`rights_cleared`；
- 视频/WAV/VTT/TXT 相对路径、字节数和 SHA-256；
- yt-dlp、字幕分组和编码 profile 版本。

短片额外带父视频/job、父媒体/字幕 SHA、`start_ms`、`end_ms`、cue 数和确定性 `clip_id`。

## 10. 测试证据

新增聚焦测试覆盖 profile、YouTube URL、清单、字幕选择、人工/自动去重、VTT、时长、路径、权利证据、AI 字段、原子写、空 audit、坏 sidecar、父 bundle 修复、合成视频切片和完整 audit。

隔离 worktree 最终执行：

```bash
AUDIOSPIDER_MIN_DISK_FREE_BYTES=1 \
PYTHON_BIN=/root/miniforge3/envs/audiospider/bin/python \
bash scripts/test.sh
```

结果：

- 80 个单元/集成测试全部通过；
- Python 全仓 compileall 通过；
- `pip check` 无损坏依赖；
- doctor 的 Python、依赖、ffmpeg、目录和磁盘检查通过；
- worktree 没有本地数据库，doctor 只给预期提醒；
- 37 个 Markdown 文件链接检查通过；
- 4 个 Shell 脚本语法检查通过。

隔离 worktree 位于约 17.4 GiB 可用的系统分区，因此测试时只把 doctor 的最小磁盘阈值临时设为 1 byte；真实下载始终写入约 130 TB 的正式数据盘路径，没有降低生产下载安全水位。

fast-forward 部署到正式仓库后再次执行未降低阈值的 `scripts/test.sh`：80 个测试仍全部通过。正式 doctor 实测：

- 下载目录 `/root/code/github_repos/AudioSpider-fork/downloads` 可写；
- 可用空间 29,413.3 GiB，高于 20 GiB 安全水位；
- 正式 SQLite `quick_check` 完整，原音频队列记录数保持 5,634；
- Python、依赖、ffmpeg、文档和 Shell 检查均通过，无提醒。

最后使用正式 main 上的 `youtube_dataset.py` 重跑数据 audit，仍为 110 个 bundle、`failure_count=0`、`incomplete_staging_count=0`。

## 11. 仍需人工完成的标注

当前 110 个 bundle 全部：

- `speaker_count=null`；
- `speaker_count_status=needs_review`；
- `ai_generation.status=unknown`；
- `rights.status=needs_review`；
- `rights_cleared=false`。

这不是遗漏，而是避免伪造确定性。下一阶段若要形成正式训练集，应由人工/法务分别核验说话人数、AI 来源声明、许可范围和允许用途，再在清单中填入证据并重建相应 sidecar。
