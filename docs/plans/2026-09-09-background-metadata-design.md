# 背景信息与文本资产设计

## 需求

下载音频时同时保存来源公开提供的背景信息、描述文本、封面、章节、字幕/transcript 和公版原文链接；支持已有音频补齐；不把平台文本与 ASR 混淆。

## 数据流

```text
Spider/API/RSS
      |
      v
AudioRecord 公共字段 + metadata_json
      |
      v
SQLite 加法式迁移与重复记录元数据回填
      |
      v
Downloader 完成音频
      |
      +--> description.txt/html
      +--> cover.*
      +--> transcript.*
      +--> chapters.json
      +--> source-text.*
      |
      v
扩展 sidecar（资产状态、哈希、来源与抓取时间）
```

## 非功能要求

- 兼容现有数据库与 sidecar。
- 背景资产失败不影响音频 done。
- 支持并发但对每条记录限制数量与总字节。
- 阻止私网、危险重定向、错误 MIME 和超大响应。
- 不保存签名 URL 查询参数到 sidecar。
- 真实来源变化要以缺失字段而非伪造字段表示。

## 来源策略

- RSS：完整解析标准、iTunes、content、media 与 Podcasting 2.0 标签。
- 小宇宙：优先解析公开 `__NEXT_DATA__`，regex 仅作回退。
- 喜马拉雅：保存 baseInfo 已公开字段，不探测隐藏 transcript。
- LibriVox：使用 extended/coverart API，保存公版原文链接。
- B站：获取公开视频详情、分P和公开字幕；无字幕时保持空数组。

## 既有数据

重新运行受控来源采集以按 URL/source_id 回填 metadata；随后对 done 且有物理文件的记录执行 background 命令，不重复下载音频。
