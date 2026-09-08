# 音频背景信息与文本采集研究

## 结论

AudioSpider 可以保存比现有 sidecar 丰富得多的背景资料，但“每条音频都有逐字稿”不成立。正确做法是：

1. 完整保存来源公开给出的描述、作者、节目/专辑、网页、封面、标签、章节和许可信息。
2. 下载来源明确链接的 transcript、字幕、章节和公版原文。
3. 将平台原文和未来模型生成的 ASR 严格区分。
4. 对没有公开文本的记录明确标记 `not_provided`，不猜测、不绕过登录。

## 标准与公开能力

RSS 2.0 的 item 原生支持 title、description、author、category、link、guid、pubDate 与 enclosure，并允许通过 XML namespace 扩展字段。[RSS Advisory Board 规范](https://www.rssboard.org/rss-specification)

Podcasting 2.0 的 `podcast:transcript` 可以链接纯文本、HTML、WebVTT、JSON 或 SRT，并允许声明语言与 captions 关系；`podcast:chapters` 可以链接外部 JSON 章节；`podcast:person` 和 `podcast:license` 可描述参与者角色与内容许可。[Transcript](https://podcasting2.org/docs/podcast-namespace/tags/transcript)、[Chapters](https://podcasting2.org/docs/podcast-namespace/tags/chapters)、[Person](https://podcasting2.org/docs/podcast-namespace/tags/person)、[License](https://podcasting2.org/docs/podcast-namespace/tags/license)

Apple 要求 RSS 提供稳定 GUID、enclosure、节目元数据和封面，并支持发布者提交 transcript 和 chapters。Apple 自己生成的 transcript 会标记为自动生成，但官方没有提供通用公共 RSS 地址供第三方批量下载，因此实现只抓发布者明确链接的文本。[Apple RSS 要求](https://podcasters.apple.com/support/823-podcast-requirements)、[Apple transcripts](https://podcasters.apple.com/support/5316-transcripts-on-apple-podcasts)、[Apple chapters](https://podcasters.apple.com/support/5482-using-chapters-on-apple-podcasts)

LibriVox 官方 API 的 `extended=1` 与 `coverart=1` 可返回 description、作者、章节/朗读者、题材、封面、版权年份和 `url_text_source`；后者常指向 Project Gutenberg 公版原文。[LibriVox API Info](https://librivox.org/api/info)、[LibriVox API 源码](https://github.com/LibriVox/librivox-public/blob/master/application/libraries/Librivox_API.php)

B站官方开放平台采用开发者注册和授权模型；当前仓库所用匿名网页接口应视为不稳定、best-effort 数据源，不能通过登录绕过、验证码或私有接口来追求完整率。[B站开放平台](https://open.bilibili.com/doc)、[开发者服务协议](https://open.bilibili.com/agreement/developer-service)

## 当前服务器实测

| 来源 | 可获得背景信息 | 文本现实情况 |
|---|---|---|
| Podcast RSS | 单集/节目简介、作者、网页、封面、分类、发布时间、显式标记、扩展标签 | 当前 4 个固定 feed 均有部分描述，但没有 transcript/chapters |
| 小宇宙 | 单集简介、时间轴、节目简介、作者、主播列表、标签、赞助、封面 | transcript 对象抽样只有 mediaId，没有公开正文 URL |
| 喜马拉雅 | 专辑、分类、封面、简介、创建时间、用户和免费/付费状态 | 抽样简介为空，无稳定公开 transcript |
| LibriVox | 书籍简介、作者、译者、章节、朗读者、题材、封面、原文链接 | 有公版原文链接，但不保证逐句对齐 |
| B站 | 视频简介、UP主、封面、发布时间、分P、权限与统计、字幕入口 | 抽样公开视频字幕列表为空；只在公开字幕存在时下载 |

## 方案比较

### 全部做成数据库列

查询方便，但五个来源字段差异很大，表结构会持续膨胀，来源升级频繁触发迁移。

### 只保存一个 JSON

适配快，但常用字段难查询，旧脚本和数据库查看器无法方便筛选。

### 公共列 + 版本化 JSON + 相邻资产（采用）

公共列保存 webpage、description、author、cover；`metadata_json` 保存来源特有结构；封面、transcript、chapters、source text 下载到音频旁。它在可查询性、向后兼容和扩展成本之间最平衡。

## 安全与真实性

- 辅助 URL 使用与音频相同的公网 DNS、重定向和 SSRF 检查。
- 限制单资产大小、单条记录资产总量和资产数量。
- 只允许文本、JSON、字幕和图片 MIME，不保存脚本或可执行响应。
- 辅助资产失败不把已验证音频改成 failed。
- sidecar 不保存带查询参数的签名媒体 URL；数据库可暂存获取资产所需 URL。
- 每份文本记录 `text_source`、抓取时间、MIME、哈希和原始/ASR属性。
- 不根据“可访问”推断版权或训练许可。

## 明确不在本次范围

本次不自动为全部音频运行 ASR。ASR 是派生数据，需要单独选择模型、语言、时间戳粒度、说话人处理和计算预算；实现会为未来 `text_source=asr` 预留兼容结构。
