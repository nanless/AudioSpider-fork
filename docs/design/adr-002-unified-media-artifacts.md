# ADR-002：统一 SQLite 媒体任务与多形态 Artifact Handler

## 状态

Accepted，2026-09-10。统一队列、产物分派和历史 bundle 迁移工具已实现。

## 背景

普通音频已经使用 `collect.py -> audiospider.db -> main.py`。B站视频和 YouTube
完整视频后来各自增加 standalone dataset CLI，分别拥有 manifest、下载、staging、
字幕和 audit。三条管线造成：

- 用户必须记住不同入口；
- SQLite 无法统一展示所有待处理/失败/完成媒体；
- lease、恢复、限额和日志策略重复；
- 正式下载目录和 sidecar 语义容易漂移；
- B站在旧 Spider 中仍默认音频，而保留画面的需求绕开正式队列；
- YouTube 历史 clip 行为可能被误当成默认正式产物。

约束是单机优先、保留现有普通音频兼容性、不得破坏正式数据库和 downloads，并且
不同来源的产物闭包不能被强行压成同一种文件。

## 决策

采用唯一正式编排链：

```text
collect.py -> audiospider.db -> main.py -> downloads/<source>/<category>/
```

SQLite 中的逻辑任务统一称为 `MediaJob`，用 `artifact_kind` 区分：

- `audio`：单音频文件及 sidecar/可选背景资产；
- `video_bundle`：完整 MP4、WAV、字幕集合和 `metadata.json` 的原子目录闭包。

实现保留 `audio_urls` 表名，回填全部旧记录为 `audio`，并在一个 SQLite 事务内
重建 URL/job partial unique 索引，使同一视频的不同 `job_key` 可共存。`main.py` 保留统一
领取、lease、过滤、重试和统计，再按 artifact kind
路由 handler。B站新任务默认 `video_bundle`；YouTube 新任务只创建完整母视频
`video_bundle`。普通音频行为保持不变。

standalone `bilibili_dataset.py` 和 `youtube_dataset.py` 的平台解析、字幕转换和 bundle
audit 能力由 `media_artifacts.py` 复用；这些 CLI 只作为兼容/诊断工具，不是正式主路径。

所有 artifact 共享 sidecar 公共语义、安全边界、rights/AI/provenance 和文件闭包
原则，但保留各自已经过测试的 schema 与平台字段。

## 后果

### 正面

- 用户只需理解 collect、数据库和 main 三段式流程；
- 所有任务可统一统计、限流、恢复和重试；
- B站视频默认保留画面，不再因旧音频默认产生语义偏差；
- YouTube 的正式产物稳定为完整母视频；
- 普通音频无需改目录或使用方式；
- 字幕、rights、AI 来源和媒体闭包进入同一审计模型；
- 已有 bundle 可在 audit 后登记，不必重新下载。

### 负面

- `audio_urls` 物理表名在过渡期与实际内容不完全一致；
- `main.py` 与 Storage 的 artifact-aware 路由和查询增加了维护面；
- 视频任务比音频任务包含更多中间状态和失败模式；
- 兼容期要同时维护正式主路径与 standalone CLI；
- SQLite 单写者限制仍然存在。

### 中性

- 音频 sidecar 仍与音频同名，视频 sidecar 仍位于 bundle 内；统一的是 envelope，
  不是强制相同文件名；
- B站字幕登录门禁和 YouTube 字幕选择仍是各自平台事实；
- 历史 clip 可以保留在 archive，但不会进入新任务默认图。

## 备选方案

### 1. 保留三个完全独立 CLI

拒绝。实现成本短期最低，但状态、目录、安全和用户心智继续分裂。

### 2. 让 `main.py` 调用 standalone CLI 子进程

拒绝作为终态。它能快速串联，但会产生嵌套编排、重复 manifest、状态提交边界不清和
凭据环境泄漏风险。可作为极短迁移桥，但必须有退出日期。

### 3. 每种 artifact 使用独立 SQLite 表

拒绝作为主设计。文件 schema 可以不同，但调度状态相同；分表会重新制造多队列统计
和领取逻辑。平台特有字段进入版本化 JSON即可。

### 4. 立即建立全新的 `media_jobs` 表并删除 `audio_urls`

暂缓。语义最整洁，但对已有正式数据、工具和报告破坏较大。先加法迁移并建立逻辑
抽象，稳定后再单独 ADR 评估物理表重命名。

### 5. 把所有视频拆成多个普通音频行

拒绝。视频、音频、字幕和 sidecar 必须作为一个可审计闭包提交；多行无法保证原子
完整性，也会丢失父媒体身份。

## 安全与合规影响

- 登录 Cookie 不进入数据库和 artifact spec；
- signed URL 不进入幂等键、sidecar 或日志；
- `rights.status` 默认 `needs_review`；
- caption provenance 与 `ai_generation` 独立；
- 公开可访问不代表训练或再分发授权；
- 新 handler 必须复用 host allowlist、DNS/redirect 检查、大小和磁盘水位。

## 迁移与回滚

迁移采用加法式 schema、SQLite backup、dry-run 和分批对账。旧音频记录不重写路径。
已完成视频 bundle 只有在 audit 通过后才登记 done。

若新 handler 出现问题：

1. 停止新 `video_bundle` 采集；
2. 让 `main.py` 只消费 `audio`；
3. 保留新增列和已登记任务，不做破坏性降级；
4. 修复后按稳定 ID重试；
5. 不复制或覆盖正式数据库。

## 参考

- [统一媒体流水线设计](../plans/2026-09-10-unified-media-pipeline-design.md)
- [统一媒体流水线实施计划](../plans/2026-09-10-unified-media-pipeline.md)
- [ADR-001：背景元数据混合存储](adr-001-background-metadata.md)
- [Sidecar Schema](../SIDECAR-SCHEMA.md)
