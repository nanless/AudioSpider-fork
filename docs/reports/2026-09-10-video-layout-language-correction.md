# 2026-09-10 视频目录与字幕语言纠偏验收

## 用户要求

- B 站视频必须放在仓库 `downloads/` 下。
- YouTube 只保留完整视频，不生成 clip。
- 英文视频使用英文字幕；中文/粤语视频使用中文或粤语字幕；其他语言同样使用本语言字幕。

## 数据迁移

B 站正式数据已从误建目录：

```text
/root/code/github_repos/AudioSpider-fork/datasets/bilibili-video-20260910
```

迁移到：

```text
/root/code/github_repos/AudioSpider-fork/downloads/bilibili-video-20260910
```

迁移后为 16 个完整 bundle、188,155,159 bytes，严格审计 `failure_count=0`、`incomplete_staging_count=0`。

## YouTube 正式数据

正式目录仍为：

```text
/root/code/github_repos/AudioSpider-fork/downloads/youtube-staging-20260909
```

当前只保留 7 个完整父视频，1,427,247,274 bytes，正式 clip 数为 0：

| 内容语言 | 字幕语言 | 父视频数 |
|---|---|---:|
| 英文 | `en` | 4 |
| 英文 | `en-US` | 1 |
| 日语 | `ja` | 1 |
| 韩语 | `ko` | 1 |

严格审计 `failure_count=0`、`clips.count=0`、`incomplete_staging_count=0`。韩语父字幕尾部比媒体长 1.613 秒，仍作为既有 provenance warning 保留。

## 可恢复归档

没有删除任何已有媒体：

```text
/root/code/github_repos/AudioSpider-fork/archive/youtube-clips-20260910
/root/code/github_repos/AudioSpider-fork/archive/youtube-language-mismatch-20260910
```

- 101 个 v2 clips 与旧 v1 clips：298,529,665 bytes。
- 粤语配英文字幕、中文配英文字幕的两个父 bundle：162,965,163 bytes。

归档不属于正式审计集，除非用户以后明确要求恢复。

## 中文字幕重查

对 `hNc-8ZGFJWU` 和 `0p93BargoME` 按中文/粤语字幕清单重新 inspect，但服务器 YouTube 出口返回 `Network is unreachable`。因此没有把旧英文字幕重新放回正式集，也没有声称已取得中文字幕。

## 代码门禁

- YouTube manifest 拒绝跨语言字幕 fallback。
- 下载后再次验证选中字幕与内容语言一致。
- bundle audit 再次验证实际字幕语言。
- `clip` CLI 默认拒绝，必须明确添加 `--allow-clips`。
- B 站空 `languages` 现在表示依据 `content_language` 生成本语言候选，不再表示所有语言。

最终验证：全仓库 113 个测试通过，45 个 Markdown 文件链接检查通过，依赖检查通过；生产下载目录剩余 24,515.9 GiB，SQLite 5,634 条记录完整。
