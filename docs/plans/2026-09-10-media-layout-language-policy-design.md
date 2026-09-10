# 视频目录与字幕语言纠偏设计

## 用户要求

1. B 站视频统一放在仓库的 `downloads/` 下。
2. YouTube 只保存完整父视频，不生成短 clip。
3. 视频与字幕语言一致：英文视频选英文字幕；中文/粤语视频选中文字幕；日语和韩语视频同样只选本语言字幕。

## 设计

- 将现有 B 站数据集原子迁移到 `downloads/bilibili-video-20260910`，保持 bundle 内相对路径和 SHA-256 不变。
- 将 YouTube `screen_clips` 和旧 v1 clip 目录移到 `archive/youtube-clips-20260910`，不删除，正式 YouTube 数据集只留下 `interviews` 和 `screen_sources` 父视频。
- 在 YouTube manifest 校验、下载后选轨确认和 bundle audit 三处执行同一语言策略。`en→en`；`zh/zh-Hans/zh-Hant/yue→zh 或 yue`；其他语言按主语言代码一致。
- `clip` CLI 保留为底层兼容能力，但必须额外提供 `--allow-clips`；未明确授权时调用会失败。
- B 站清单不再用“所有语言”作为默认示例，中文内容明确列中文/粤语字幕代码。
- 文档、验收报告和两个数据集 Skill 全部改成真实 `downloads/` 路径及“父视频模式”。

## 数据处理边界

已有 clip 只做可恢复移动，不删除。字幕错配的两个 YouTube 父 bundle 移到归档；只有真实探针证明存在中文平台字幕时才重新加入正式集，否则保持排除并如实报告。
