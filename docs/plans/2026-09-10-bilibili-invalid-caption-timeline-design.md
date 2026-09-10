# B 站错配字幕时间轴设计

## 背景与决策

实际回填发现，平台有时会对某个分 P 返回属于其他分 P 或整段视频的自动字幕。
样本视频只有 8.576 秒和 9 秒，字幕却分别延伸到 210.4 秒和 1225.79 秒。
这不是可以靠放宽浮点误差处理的轻微偏差。系统因此不放宽原有 2 秒边界，
不截断平台字幕，也不把错配文本冒充成可用转录。

对 `require_caption=false` 的 best-effort 任务，完整 MP4/WAV 继续保留，
`caption.status` 记为 `invalid_timeline`，`tracks` 为空。为了保留平台原始信息，
错配轨道净化后的平台 JSON 以 `.rejected.json` 隔离命名并进入哈希闭包，但不生成会被误当成
可对齐文本的 VTT/TXT。`rejected_tracks[].timeline_rejection` 保存媒体时长、最大 cue
结束时间、超出秒数、容差和版本化规则；不保存签名 URL 或 Cookie。
若同一视频另有合法轨道，合法轨道继续完整保存，顶层状态为 `downloaded`、
`payload_status=partial`。对 `require_caption=true` 的严格任务，至少有一条可用轨道才成功。

正常下载与字幕-only 回填共用同一个逐轨获取、解析、分类和临时落盘函数。
验收器检查状态、文件闭包、数值关系、超出阈值和库存中的已选轨道身份，
从而保证“有字幕但不可用”与“平台没有字幕”两种语义不被混淆。
