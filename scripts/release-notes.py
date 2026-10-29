#!/usr/bin/env python3
"""发布说明生成器：从 CHANGELOG.md 提取「版本区间」的条目。

区间语义 (since, version]；不传 --since 表示覆盖到当前版本为止的全部条目。
找不到条目时以非 0 退出码失败，避免发出空 Release。

用法：
    python scripts/release-notes.py --version 0.2.0 --since 0.1.0 --out RELEASE_NOTES.md
    python scripts/release-notes.py --version 0.2.0            # 打印到 stdout
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HEADING = re.compile(r"^##\s+(.+?)\s*$")
VERSION_IN_HEADING = re.compile(r"^v?(\d+\.\d+\.\d+)")


def parse_version(text: str, label: str) -> tuple[int, int, int]:
    match = SEMVER.match(str(text).strip())
    if not match:
        raise SystemExit(f"{label} 不是合法版本号：{text}（示例：0.2.0）")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_changelog(text: str) -> list[tuple[tuple[int, int, int], str, str]]:
    """返回 [(version_tuple, version_str, body)]，保持文件出现顺序。"""
    sections: list[tuple[tuple[int, int, int], str, str]] = []
    current_version: tuple[int, int, int] | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            version_match = VERSION_IN_HEADING.match(heading.group(1))
            if version_match:
                if current_version:
                    sections.append((current_version, ".".join(map(str, current_version)), "\n".join(current_body).strip()))
                current_version = parse_version(version_match.group(1), "CHANGELOG 标题")
                current_body = []
                continue
            if current_version:
                sections.append((current_version, ".".join(map(str, current_version)), "\n".join(current_body).strip()))
            current_version, current_body = None, []
            continue
        if current_version:
            current_body.append(line)
    if current_version:
        sections.append((current_version, ".".join(map(str, current_version)), "\n".join(current_body).strip()))
    return sections


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 CHANGELOG.md 生成发布说明")
    parser.add_argument("--version", required=True, help="本次发布版本号，如 0.2.0")
    parser.add_argument("--since", default="", help="只包含版本号大于它的条目（通常是上一个 tag）")
    parser.add_argument("--out", default="", help="写入文件；缺省打印到 stdout")
    args = parser.parse_args(argv)

    version = parse_version(args.version, "--version")
    since = parse_version(args.since, "--since") if args.since else None

    if not CHANGELOG.is_file():
        raise SystemExit(f"找不到 CHANGELOG.md：{CHANGELOG}")

    sections = parse_changelog(CHANGELOG.read_text(encoding="utf-8"))
    selected = [
        section for section in sections
        if section[0] <= version and (since is None or section[0] > since)
    ]
    selected.sort(key=lambda section: section[0], reverse=True)
    if not selected:
        scope = f"{args.since} 之后到 {args.version}" if since else args.version
        raise SystemExit(f"CHANGELOG.md 里没有 {scope} 的条目：拒绝生成空发布说明。")

    header = f"v{args.since} → v{args.version}" if since else f"v{args.version}"
    lines = [f"## 变更范围 {header}", ""]
    for _, version_text, body in selected:
        lines.extend([f"## {version_text}", "", body, ""])
    lines.extend([
        "## 安装",
        "",
        "1. 下载本页 `astrbot_plugin_model_websearch-v*.zip`；",
        "2. AstrBot WebUI → 插件管理 → 从文件安装（或解压到 `plugins/` 下的 `astrbot_plugin_model_websearch/`）；",
        "3. 重启 / 重载插件；",
        "4. 在 AstrBot「AI 能力 → 网页搜索」选择「跟随当前聊天模型（模型原生搜索）」即可，",
        "   也可以直接用 model-console（模型控制台）勾选并测试。",
        "",
    ])
    output = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"已写入 {args.out}（{len(selected)} 个版本条目）")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
