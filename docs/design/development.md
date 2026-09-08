# 开发与测试

## 开发环境

```bash
bash scripts/bootstrap_conda.sh
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate audiospider
python doctor.py
```

运行统一检查：

```bash
bash scripts/test.sh
```

它依次执行单元测试、Python 编译、依赖一致性、环境诊断、Markdown 链接检查和 Shell 语法检查。

## 代码分层

| 位置 | 职责 |
|---|---|
| `main.py`、`collect.py`、`discover.py` | CLI 与流程编排 |
| `spiders/` | 外部来源适配 |
| `storage.py` | Schema、迁移、领取、状态与查询 |
| `downloader.py` | 传输、校验、转码、落盘 |
| `network_safety.py` | URL、DNS 和重定向安全 |
| `config.py` | 路径、默认值、资源限制、来源配置 |
| `doctor.py`、`probe.py` | 诊断与有界真实测试 |
| `tests/` | 离线自动化回归 |

修改时尽量把逻辑放在所属层，不在 CLI 中复制实现。

## 测试层次

### 1. 静态与单元测试

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q .
python -m pip check
python scripts/check_docs.py
```

单元测试应默认离线、确定、可重复。HTTP 下载测试使用本地临时服务；数据库测试使用临时目录。

### 2. 环境测试

```bash
python doctor.py
python doctor.py --json
```

这验证真实解释器、安装包、ffmpeg/libopus、磁盘和正式数据库，但不访问外网。

### 3. 真实来源探针

```bash
python probe.py --source podcast_rss --feeds 1 --episodes 2
```

真实探针是集成测试，不应加入默认离线测试套件。记录日期、服务器、来源、命令、结果条数与失败原因；外部站点成功不保证未来成功。

### 4. 端到端下载

先向临时或明确测试数据库写入 1 条可授权的媒体任务，再用 1 worker 下载 original，验证音频、JSON、SHA-256 和状态。避免把正式数据库作为破坏性测试夹具。

## 新增 Spider

1. 在 `spiders/` 新建模块并继承 `BaseSpider`。
2. 把响应映射为 `AudioRecord`。
3. 设置稳定 `source_id` 和可解释的元数据。
4. 对列表、详情、分页、空结果、异常和上限写测试。
5. 在 `collect.ALL_SPIDERS` 和 `SPIDER_CONFIGS` 注册。
6. 在 `probe.py` 添加严格上限。
7. 更新来源参考、CLI 参考和 README。
8. 先离线测试，再运行单来源探针。

不得通过自动绕过验证码、登录、DRM 或访问控制来让测试通过。

## 修改数据库

Schema 变更必须是向后兼容的加法式迁移：

- 新列提供默认值。
- 使用 `_add_column_if_missing`。
- 创建必要索引。
- 对旧库与新库都写测试。
- 在数据库参考和 `CHANGELOG.md` 记录。

不要假设用户会删除旧数据库。迁移前后应运行 `PRAGMA quick_check` 并验证记录计数。

## 修改下载器

下载相关变更至少覆盖：

- 正常 200。
- 有效与无效 Range/Content-Range。
- 416。
- 重定向和不安全目标。
- 超大小、磁盘安全水位。
- HTML/JSON 错误体。
- ffprobe 失败。
- Opus 转码失败。
- 同内容哈希去重。
- 异常中断后的文件与数据库状态。

任何让失败被误报为 done 的回归都属于高优先级问题。

## 文档约定

- 根 README 面向第一次使用者。
- `docs/getting-started/` 教程按步骤完成目标。
- `docs/guides/` 围绕运维任务。
- `docs/reference/` 精确描述参数和字段。
- `docs/design/` 解释架构、边界和开发决策。

文档用相对链接，新增或改名后运行 `python scripts/check_docs.py`。示例命令必须注明是否访问外网、写正式数据库或下载大文件。

## 提交前清单

```bash
git diff --check
bash scripts/test.sh
git status --short
```

然后人工确认：

- 没有提交 `audiospider.db`、`downloads/`、`logs/`、`tmp/`。
- 没有 Secret、Cookie 或签名 URL。
- 变更的 CLI、Schema 和 Spider 有对应文档。
- 真实测试规模有明确上限。
- 已记录不可达外部来源，未把网络问题伪装成测试通过。
