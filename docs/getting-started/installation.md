# 从零安装

## 1. 系统要求

- Linux 或 macOS
- Python 3.10–3.12，推荐 3.11
- Conda/Miniforge/Miniconda
- ffmpeg 和 ffprobe
- 足够的磁盘空间

`dev_L4_1gpus` 已有 `/root/miniforge3/bin/conda` 和系统 ffmpeg。

## 2. 获取代码

```bash
cd ~/code/github_repos
git clone https://github.com/nanless/AudioSpider-fork.git
cd AudioSpider-fork
```

如果仓库已经存在：

```bash
cd ~/code/github_repos/AudioSpider-fork
git status --short --branch
```

升级前不要在有未提交代码时直接 pull。

## 3. 创建 Conda 环境

推荐：

```bash
bash scripts/bootstrap_conda.sh
```

手工方式：

```bash
conda env create -f environment.yml
```

激活：

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate audiospider
```

确认 Python：

```bash
which python
python --version
```

## 4. 安装 ffmpeg

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

确认：

```bash
ffmpeg -version
ffprobe -version
ffmpeg -encoders | grep libopus
```

如果只使用 `--format original`，下载时不转码，但项目测试和离线转换仍需要 ffmpeg。

## 5. 配置可选凭据

Apple Podcasts 不需要凭据。Podcast Index 需要：

```bash
export PODCAST_INDEX_KEY="..."
export PODCAST_INDEX_SECRET="..."
```

不要把真实 Key 写进 `.env.example` 或提交到 Git。

## 6. 运行诊断

```bash
python doctor.py
```

数据库尚未创建时，doctor 会给出提醒而不是环境失败。首次执行 `main.py stats` 或正式采集后会自动建库。

## 7. 运行测试

```bash
bash scripts/test.sh
```

测试通过后再运行真实探针。下一步见[第一次运行](first-run.md)。
