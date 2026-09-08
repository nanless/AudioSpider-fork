# Copyright (c) 2026 Hao Yin. All rights reserved.

"""Opus 转 WAV：默认 24kHz 16bit 单声道

用法:
  python opus_to_wav.py input.opus                    转换单个文件
  python opus_to_wav.py dir/                          转换目录下所有 .opus
  python opus_to_wav.py dir/ --recursive              递归子目录
  python opus_to_wav.py dir/ --output-dir out/        指定输出目录
  python opus_to_wav.py dir/ --workers 8              并发数（默认 4）
  python opus_to_wav.py input.opus --dry-run          只预览不转换
"""

import argparse
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def convert_one(src: str, output_dir: str | None, dry_run: bool,
                relative_root: str | None = None) -> dict:
    """转换单个 .opus 文件为 24kHz 单声道 PCM16 WAV。"""
    result = {"path": src, "status": "skipped", "detail": ""}

    if not src.lower().endswith(".opus"):
        result["detail"] = "非 .opus 文件"
        return result

    if output_dir:
        relative = os.path.relpath(src, relative_root) if relative_root else os.path.basename(src)
        dst = os.path.join(output_dir, os.path.splitext(relative)[0] + ".wav")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    else:
        dst = os.path.splitext(src)[0] + ".wav"

    if os.path.exists(dst):
        result["detail"] = f"目标已存在: {dst}"
        return result

    if dry_run:
        size_mb = os.path.getsize(src) / 1024 / 1024
        result["status"] = "will_convert"
        result["detail"] = f"→ {dst} ({size_mb:.2f}MB)"
        return result

    tmp = f"{dst}.tmp.{uuid.uuid4().hex}.wav"
    try:
        cmd = ["ffmpeg", "-y", "-i", src, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if proc.returncode != 0:
            if os.path.exists(tmp):
                os.remove(tmp)
            result["status"] = "failed"
            result["detail"] = proc.stderr[-200:] if proc.stderr else "ffmpeg 返回非零"
            return result

        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            if os.path.exists(tmp):
                os.remove(tmp)
            result["status"] = "failed"
            result["detail"] = "输出文件为空"
            return result

        os.replace(tmp, dst)
        src_size = os.path.getsize(src) / 1024 / 1024
        dst_size = os.path.getsize(dst) / 1024 / 1024
        result["status"] = "converted"
        result["detail"] = f"→ {dst} ({src_size:.2f}MB → {dst_size:.2f}MB)"
        return result

    except subprocess.TimeoutExpired:
        if os.path.exists(tmp):
            os.remove(tmp)
        result["status"] = "failed"
        result["detail"] = "ffmpeg 超时"
        return result
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        result["status"] = "failed"
        result["detail"] = str(e)
        return result


def collect_files(path: str, recursive: bool) -> list[str]:
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".opus") else []

    files = []
    if recursive:
        for root, _, names in os.walk(path):
            for name in names:
                if name.lower().endswith(".opus"):
                    files.append(os.path.join(root, name))
    else:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if os.path.isfile(full) and name.lower().endswith(".opus"):
                files.append(full)
    files.sort()
    return files


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def main():
    parser = argparse.ArgumentParser(description="Opus → 24kHz 单声道 PCM16 WAV")
    parser.add_argument("input", help="输入文件或目录")
    parser.add_argument("--output-dir", help="输出目录（默认与源文件同目录）")
    parser.add_argument("--recursive", "-r", action="store_true", help="递归子目录")
    parser.add_argument("--workers", type=_positive_int, default=4,
                        help="并发数（默认 4）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际转换")
    args = parser.parse_args()

    if not check_ffmpeg():
        print("错误: 未找到 ffmpeg，请先安装: brew install ffmpeg")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"错误: 路径不存在: {args.input}")
        sys.exit(1)

    files = collect_files(args.input, args.recursive)
    if not files:
        print("没有找到 .opus 文件。")
        return

    print(f"找到 {len(files)} 个 .opus 文件")
    if args.dry_run:
        print("[ 预览模式 ]\n")

    stats = {"converted": 0, "skipped": 0, "failed": 0, "will_convert": 0}
    relative_root = args.input if os.path.isdir(args.input) else None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(convert_one, f, args.output_dir, args.dry_run,
                        relative_root): f
            for f in files
        }
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            stats[r["status"]] = stats.get(r["status"], 0) + 1
            icon = {"converted": "✓", "skipped": "·", "failed": "✗", "will_convert": "→"}.get(r["status"], "?")
            print(f"  [{i}/{len(files)}] {icon} {os.path.basename(r['path'])}  {r['detail']}")

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
