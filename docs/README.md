# AudioSpider 文档中心

这套文档按读者经验分成四层。第一次使用时从上往下读，不需要一次看完全部内容。

## 第 0 层：项目入口

- [根目录 README](../README.md)：项目是什么、十分钟上手、常用命令、安全限制。

## 第 1 层：新手教程

目标是“照着做就能运行”。

- [核心概念](getting-started/concepts.md)
- [从零安装](getting-started/installation.md)
- [第一次运行](getting-started/first-run.md)

## 第 2 层：任务指南

目标是“知道要完成什么任务，查对应步骤”。

- [发现与采集](guides/discovery-and-collection.md)
- [下载与转码](guides/download-and-convert.md)
- [运行与维护](guides/operations.md)
- [故障排查](guides/troubleshooting.md)

## 第 3 层：接口参考

目标是“查清每个参数、字段和适配器的准确含义”。

- [CLI 命令参考](reference/cli.md)
- [配置与环境变量](reference/configuration.md)
- [数据库结构](reference/database.md)
- [来源适配器](reference/spiders.md)
- [背景信息与文本资产](reference/background-metadata.md)

## 第 4 层：设计与开发

目标是“修改代码或评审系统设计”。

- [系统架构](design/architecture.md)
- [安全边界](design/security.md)
- [数据流与状态机](design/data-flow.md)
- [开发与测试](design/development.md)
- [ADR-001：背景信息混合存储](design/adr-001-background-metadata.md)
- [安全问题报告](../SECURITY.md)
- [变更记录](../CHANGELOG.md)

## 第 5 层：验收记录

目标是“看到具体机器、日期、命令范围和实际结果”。

- [2026-09-08 dev_L4_1gpus 服务器验收报告](reports/2026-09-08-dev-l4-validation.md)

## 深度研究

- [2026-09-09 音频背景信息与文本采集研究](research/2026-09-09-background-metadata-research.md)

## 推荐阅读路线

### 我只想先跑起来

1. [从零安装](getting-started/installation.md)
2. [第一次运行](getting-started/first-run.md)
3. [下载与转码](guides/download-and-convert.md)

### 我要长期跑任务

1. [运行与维护](guides/operations.md)
2. [数据库结构](reference/database.md)
3. [安全边界](design/security.md)
4. [故障排查](guides/troubleshooting.md)

### 我要新增站点

1. [来源适配器](reference/spiders.md)
2. [系统架构](design/architecture.md)
3. [数据流与状态机](design/data-flow.md)
4. [开发与测试](design/development.md)

## 文档验证

文档变更合并前运行：

```bash
python scripts/check_docs.py
```

它会检查根 README 和 `docs/` 中的相对链接是否指向真实文件。
