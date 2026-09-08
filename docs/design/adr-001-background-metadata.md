# ADR-001：音频背景信息采用混合存储

- 状态：Accepted
- 日期：2026-09-09

## 背景

五个来源的公共字段有交集，但字幕、章节、人物、许可、统计、付费状态和公版原文等结构不同。用户需要音频旁可直接使用的完整背景资料，同时现有 SQLite 查询和下载流程必须向后兼容。

## 决策

1. 在 `audio_urls` 增加 `webpage_url`、`description`、`author`、`cover_url` 和 `metadata_json`。
2. `metadata_json` 使用带 `schema_version` 与 `provenance` 的来源特有对象。
3. 可下载资产统一列在 `assets` 中，分为 cover、transcripts、chapters 和 source_texts。
4. 音频下载成功后，best-effort 下载辅助资产，并把结果写入主 sidecar。
5. 既有音频通过 background 命令重建 sidecar 和补下载资产，不重复下载音频。
6. 平台 transcript 与未来 ASR 通过 `text_source` 区分。

## 备选方案

- 全扁平列：放弃，因为来源差异会导致高频 Schema 迁移。
- 仅 JSON：放弃，因为常用字段难以筛选、展示和回填。
- 多张规范化关系表：暂缓，单机 244 条数据无需承担额外运维和 join 复杂度。

## 影响

优点：保持常用查询简单，来源扩展不必反复迁移；sidecar 自包含；辅助资产可独立失败和重试。

代价：JSON 内字段不能直接依赖普通索引；相邻封面可能重复；来源字段仍是 best-effort；需要限制辅助资产大小和类型。

## 安全条件

所有辅助请求必须经过公网目标校验和重定向检查；禁止自动获取需登录、DRM、验证码或未公开 transcript；sidecar 中的 URL 必须脱敏。
