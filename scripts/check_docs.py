#!/usr/bin/env python3
"""检查仓库 Markdown 相对链接是否指向现有文件。"""

from pathlib import Path
import re
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    files = sorted(ROOT.glob("*.md"))
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    return [path for path in files if path.exists()]


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
    if errors:
        print("发现无效 Markdown 链接：")
        for error in errors:
            print(f"  {error}")
        return 1
    print(f"Markdown 链接检查通过：{len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
