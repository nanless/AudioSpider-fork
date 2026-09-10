# 2026-09-10 B 站视频数据管线首批验收

## 环境与范围

- 服务器：`dev_L4_1gpus`
- 仓库：`/root/code/github_repos/AudioSpider-fork`
- Conda：`audiospider`，Python 3.11
- 数据：`/root/code/github_repos/AudioSpider-fork/datasets/bilibili-video-20260910`
- 清单：示例 1 条 + 现有 B 站音频候选回采 15 个分 P
- 画质上限：480p

## 真实结果

- 正式父 bundle：16
- MP4：16
- 16 kHz 单声道 PCM16 WAV：16
- 数据集占用：188,155,159 bytes（约 179 MiB）
- `caption.status=auth_required`：7
- `caption.status=not_provided_publicly`：9
- 真实已下载平台字幕轨：0
- `.staging`：0
- 严格审计：`failure_count=0`

这批“没有字幕文件”不是代码把字幕漏掉：匿名 player API 对 6 条明确返回需要登录，对 5 条明确返回公开轨道为空。首条样本 `BV1FD4y147uH` 的 MP4 实测为 852×480 H.264 + 44.1 kHz 双声道 AAC，314.677 秒；派生 WAV 为 16 kHz 单声道 PCM16。

## 离线字幕闭环

合成端到端测试构造带音视频的 MP4、抽取 WAV、保存平台形状的原始字幕 JSON、生成 VTT/TXT 和 sidecar。完整 bundle 通过；篡改 TXT 后 validator 按预期失败。B 站聚焦套件共 32 个测试通过，全仓库共 108 个测试通过。

## 结论边界

首批已证明公开 DASH 下载、断点状态、精确合并、WAV、sidecar 和 audit 可运行。由于未向服务器提供合法登录 Cookie，真实人工/自动字幕的下载分支尚无实网正向样本；不能把合成测试冒充真实平台字幕。程序已支持通过仅存在于进程环境的 `BILIBILI_COOKIE` 重新探测，凭据不会持久化。
