# Copyright (c) 2026 Hao Yin. All rights reserved.

"""批量音频格式转换：24kHz PCM 输入编码为单声道 Opus 32kbps

用法:
  python convert_audio.py                   转换 downloads/ 下所有音频
  python convert_audio.py --dry-run         只预览，不实际转换
  python convert_audio.py --dir path/to/dir 指定目录
  python convert_audio.py --workers 4       并发数（默认 4）
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import DB_PATH, DOWNLOAD_DIR
from storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

TARGET_EXT = ".opus"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".wma"}
FFMPEG_ARGS = [
    "-vn",                # 丢弃视频流（封面图等）
    "-ar", "24000",       # 24kHz 采样率
    "-ac", "1",           # 单声道
    "-c:a", "libopus",    # Opus 编码
    "-b:a", "32k",        # 32kbps
]


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def get_audio_info(filepath: str) -> dict:
    """用 ffprobe 获取音频信息"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", filepath],
            capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)
    except Exception:
        return {}


def is_already_target_format(filepath: str) -> bool:
    """检查文件是否已经是 Opus 单声道（解码采样率通常报告 48kHz）。"""
    if not filepath.lower().endswith(TARGET_EXT):
        return False
    info = get_audio_info(filepath)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio":
            ch = int(stream.get("channels", 0))
            if stream.get("codec_name") == "opus" and ch == 1:
                return True
    return False


def convert_file(filepath: str, dry_run: bool = False,
                 target_path: str | None = None,
                 storage: Storage | None = None) -> dict:
    """转换单个音频文件，返回结果信息"""
    ext = os.path.splitext(filepath)[1].lower()
    result = {"path": filepath, "status": "skipped", "detail": ""}

    if ext == TARGET_EXT and is_already_target_format(filepath):
        result["detail"] = "已是目标格式"
        return result

    if ext not in AUDIO_EXTENSIONS and ext != TARGET_EXT:
        result["detail"] = f"非音频文件 ({ext})"
        return result

    base = os.path.splitext(filepath)[0]
    final_output = target_path or base + TARGET_EXT
    tmp_output = f"{base}.tmp_convert.{uuid.uuid4().hex}{TARGET_EXT}"

    if dry_run:
        old_size = os.path.getsize(filepath) / 1024 / 1024
        result["status"] = "will_convert"
        result["detail"] = f"{ext} → {TARGET_EXT} ({old_size:.1f}MB)"
        return result

    if (os.path.exists(final_output)
            and os.path.realpath(final_output) != os.path.realpath(filepath)):
        result["status"] = "failed"
        result["detail"] = f"目标文件已存在，拒绝覆盖: {final_output}"
        return result

    try:
        cmd = ["ffmpeg", "-y", "-i", filepath, *FFMPEG_ARGS, tmp_output]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if proc.returncode != 0:
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            result["status"] = "failed"
            result["detail"] = proc.stderr[-200:] if proc.stderr else "ffmpeg 返回非零"
            return result

        if (not os.path.exists(tmp_output) or os.path.getsize(tmp_output) == 0
                or not get_audio_info(tmp_output).get("streams")):
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            result["status"] = "failed"
            result["detail"] = "输出文件为空"
            return result

        old_size = os.path.getsize(filepath)
        new_size = os.path.getsize(tmp_output)

        # 原文件不是 .opus → 删原文件，重命名临时文件
        # 原文件就是 .opus → 用临时文件覆盖
        os.replace(tmp_output, final_output)

        # 更新配套的 .json 元信息
        content_hash = Storage.compute_file_hash(final_output)
        _update_meta_json(filepath, final_output, content_hash)
        if storage is not None:
            storage.update_converted_file(
                filepath, final_output, new_size, content_hash,
            )
        if filepath.lower() != final_output.lower() and os.path.exists(filepath):
            os.remove(filepath)

        result["status"] = "converted"
        result["detail"] = (
            f"{ext} → {TARGET_EXT}  "
            f"{old_size / 1024 / 1024:.1f}MB → {new_size / 1024 / 1024:.1f}MB  "
            f"({new_size / old_size * 100:.0f}%)"
        )
        return result

    except subprocess.TimeoutExpired:
        if os.path.exists(tmp_output):
            os.remove(tmp_output)
        result["status"] = "failed"
        result["detail"] = "ffmpeg 超时"
        return result
    except Exception as e:
        if os.path.exists(tmp_output):
            os.remove(tmp_output)
        result["status"] = "failed"
        result["detail"] = str(e)
        return result


def _update_meta_json(old_path: str, new_path: str, content_hash: str):
    """更新配套 .json 元信息文件（路径和格式字段）"""
    old_json = os.path.splitext(old_path)[0] + ".json"
    new_json = os.path.splitext(new_path)[0] + ".json"

    if os.path.exists(old_json):
        try:
            with open(old_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["file_format"] = "opus"
            meta["file_size"] = os.path.getsize(new_path) if os.path.exists(new_path) else 0
            meta["content_hash"] = content_hash
            meta["content_hash_algorithm"] = "sha256"
            if old_json != new_json:
                os.remove(old_json)
            with open(new_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _opus_needs_convert(filepath: str) -> bool:
    """检查 .opus 文件是否需要重新转换（Opus 解码输出固定 48kHz，只能检查通道数）"""
    info = get_audio_info(filepath)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio":
            ch = int(stream.get("channels", 0))
            if ch == 1:
                return False
    return True


def scan_audio_files(directory: str) -> list[str]:
    """扫描目录下需要转换的音频文件（已符合目标格式的跳过）"""
    files = []
    start = Path(directory).resolve()
    if any((ancestor / ".audiospider-dataset.json").is_file()
           for ancestor in (start, *start.parents)):
        logger.info("跳过受 sidecar 保护的数据集目录或子目录: %s", start)
        return []
    for root, directories, filenames in os.walk(directory):
        if ".audiospider-dataset.json" in filenames:
            directories[:] = []
            logger.info("跳过受 sidecar 保护的数据集目录: %s", root)
            continue
        for fname in filenames:
            if ".tmp_conv" in fname or ".tmp_convert" in fname:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext == TARGET_EXT:
                if _opus_needs_convert(os.path.join(root, fname)):
                    files.append(os.path.join(root, fname))
                continue
            if ext in AUDIO_EXTENSIONS:
                files.append(os.path.join(root, fname))
    files.sort()
    return files


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def plan_targets(files: list[str]) -> dict[str, str]:
    """Choose deterministic, collision-free output names before concurrency."""
    base_counts = Counter(os.path.splitext(path)[0] for path in files)
    targets = {}
    for path in files:
        base, ext = os.path.splitext(path)
        if base_counts[base] > 1:
            targets[path] = f"{base}_{ext.lstrip('.').lower() or 'audio'}{TARGET_EXT}"
        else:
            targets[path] = base + TARGET_EXT
    return targets


def main():
    parser = argparse.ArgumentParser(
        description="批量音频格式转换 → 单声道 Opus 32kbps（24kHz PCM 输入）"
    )
    parser.add_argument("--dir", default=DOWNLOAD_DIR, help="音频目录 (默认 downloads/)")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际转换")
    parser.add_argument("--workers", type=_positive_int, default=4,
                        help="并发数 (默认 4)")
    args = parser.parse_args()

    if not check_ffmpeg():
        print("错误: 未找到 ffmpeg，请先安装: brew install ffmpeg")
        sys.exit(1)

    if not os.path.isdir(args.dir):
        print(f"错误: 目录不存在: {args.dir}")
        sys.exit(1)

    files = scan_audio_files(args.dir)
    if not files:
        print("没有找到音频文件。")
        return

    print(f"扫描到 {len(files)} 个音频文件")
    if args.dry_run:
        print("[ 预览模式 ]\n")

    stats = {"converted": 0, "skipped": 0, "failed": 0, "will_convert": 0}
    targets = plan_targets(files)
    storage = Storage() if os.path.exists(DB_PATH) else None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(convert_file, f, args.dry_run, targets[f], storage): f
            for f in files
        }
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            status_icon = {"converted": "✓", "skipped": "·", "failed": "✗", "will_convert": "→"}
            icon = status_icon.get(result["status"], "?")
            rel = os.path.relpath(result["path"], args.dir)
            logger.info(f"[{i}/{len(files)}] {icon} {rel}  {result['detail']}")

    print(f"\n{'=' * 50}")
    if args.dry_run:
        print(f"  需转换: {stats.get('will_convert', 0)}")
        print(f"  已跳过: {stats['skipped']}")
    else:
        print(f"  已转换: {stats['converted']}")
        print(f"  已跳过: {stats['skipped']}")
        print(f"  失败:   {stats['failed']}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
