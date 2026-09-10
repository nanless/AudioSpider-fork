#!/usr/bin/env python3
"""检查仓库 Markdown 相对链接是否指向现有文件。"""

from pathlib import Path
import re
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
FORBIDDEN_CURRENT_PHRASES = {
    "YouTube 数据不用 SQLite 音频队列": "YouTube 正式数据必须进入统一 SQLite 队列",
    "如果需要完整视频、WAV、全部平台字幕和独立 sidecar，请不要改旧音频队列，使用 `bilibili_dataset.py`":
        "B站正式完整视频必须使用 collect.py -> audiospider.db -> main.py",
    "`collect.py` 的 `ALL_SPIDERS` 注册当前五个 Spider": "当前应登记六个 Spider",
    "├── captions.zh-Hans.manual.<track-id>.json": "B站字幕文件名必须包含最终 index",
}


def markdown_files() -> list[Path]:
    files = sorted(ROOT.glob("*.md"))
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    return [path for path in files if path.exists()]


def current_semantics_files() -> list[Path]:
    return [
        path for path in markdown_files()
        if "plans" not in path.relative_to(ROOT).parts
        and "reports" not in path.relative_to(ROOT).parts
        and path.name != "CHANGELOG.md"
    ]


def check_file(path: Path) -> list[str]:
    errors = []
    in_code_block = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        for match in LINK.finditer(line):
            target = match.group(1).strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: {target}")
    return errors


def main() -> int:
    files = markdown_files()
    errors = [error for path in files for error in check_file(path)]
    for path in current_semantics_files():
        text = path.read_text(encoding="utf-8")
        for phrase, reason in FORBIDDEN_CURRENT_PHRASES.items():
            if phrase in text:
                errors.append(
                    f"{path.relative_to(ROOT)}: 过时语义：{reason}"
                )
    if errors:
        print("发现无效 Markdown 链接：")
        for error in errors:
            print(f"  {error}")
        return 1
    print(f"Markdown 链接检查通过：{len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
