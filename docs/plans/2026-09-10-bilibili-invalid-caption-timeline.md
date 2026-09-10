# Bilibili Invalid Caption Timeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在保留完整 B 站视频的同时，安全拒收与当前分 P 时长明显错配的平台字幕，并在 sidecar 中明确标注。

**Architecture:** 在 `bilibili_dataset.py` 增加逐轨时间轴诊断和共享 payload 准备函数，正常下载与历史回填共用。错配轨道净化后的平台 JSON 隔离入闭包，不生成 VTT/TXT；有其他有效轨时保留有效轨，全部错配时 best-effort 标记 `invalid_timeline`。验收器交叉检查净化 JSON、ffprobe 时长、数值证据和 inventory 身份。

**Tech Stack:** Python 3.11、`aiohttp`、SQLite、`unittest`、FFmpeg/ffprobe。

---

### Task 1: 定义时间轴拒收规则

**Files:**
- Modify: `bilibili_dataset.py`
- Test: `tests/test_bilibili_dataset.py`

**Step 1:** 写失败测试：最后 cue 超出媒体 2 秒时返回有限数值证据，未超出时返回 `None`。

**Step 2:** 运行 `python -m unittest tests.test_bilibili_dataset` 并确认新测试先失败。

**Step 3:** 实现 `caption_timeline_rejection`，校验输入是正有限时长，输出固定字段。

**Step 4:** 重跑单元测试并提交。

### Task 2: 接入正常下载和验收

**Files:**
- Modify: `bilibili_dataset.py`
- Test: `tests/test_bilibili_dataset.py`

**Step 1:** 为 `invalid_timeline` 的 sidecar 闭包、数值关系、strict/best-effort 分支写测试。

**Step 2:** 先运行测试，确认旧实现会落盘错误字幕或使 bundle 失败。

**Step 3:** 逐轨下载并检查时间轴；合法轨产生 JSON/VTT/TXT，错配轨只把 `.rejected.json` 加入闭包。

**Step 4:** 扩展 `validate_bundle` 和库存诊断交叉校验，并运行 B 站定向测试。

**Step 5:** 提交正常下载支持。

### Task 3: 接入字幕-only 回填

**Files:**
- Modify: `scripts/backfill_bilibili_captions.py`
- Test: `tests/test_bilibili_caption_backfill.py`

**Step 1:** 写回填错配字幕不生成可用 VTT/TXT、只隔离净化平台 JSON、媒体不变、sidecar/SQLite 闭包同步的失败测试。

**Step 2:** 让 `_prepare_captions` 返回有效轨和隔离轨，并保持原子更新/回滚边界。

**Step 3:** 运行回填定向测试，确认失败诊断不包含 URL 或登录信息。

**Step 4:** 提交回填支持。

### Task 4: 文档、真实回填与全量验收

**Files:**
- Modify: `docs/reference/bilibili-video-sidecars.md`
- Modify: `docs/guides/bilibili-video-datasets.md`
- Modify: `docs/reference/cli.md`
- Modify: `CHANGELOG.md`

**Step 1:** 记录 `invalid_timeline` 的语义、数值字段、strict/best-effort 差异和故障排查方法。

**Step 2:** 运行 `python -m unittest discover -s tests -v`、`pip check`、`compileall`、`doctor.py` 和 `scripts/check_docs.py`。

**Step 3:** 合入生产仓库，用授权的进程内 Cookie 重跑回填，对全部已完成 bundle 做闭包/密钥泄漏审计。

**Step 4:** 中文 commit 并 push，报告真实样本数、字幕状态和未完成下载。
