#!/usr/bin/env python3
"""把插件目录打成可安装 zip（零依赖，仅标准库）。

产物结构（AstrBot WebUI「从文件安装插件」/ 手动解压到 plugins/ 都能用）：

    astrbot_plugin_model_websearch-v0.2.0.zip
    └── astrbot_plugin_model_websearch/
        ├── main.py
        ├── client.py
        ├── tools.py
        ├── metadata.yaml
        ├── _conf_schema.json
        ├── README.md
        ├── LICENSE
        └── CHANGELOG.md

打进包里的只有**运行插件必需**的文件：tests/、scripts/、.github/ 属于开发内容，不随插件分发。
版本号从 metadata.yaml 的 `version:` 读取，避免和 tag 各写一份。

用法：
    python scripts/build-plugin-zip.py                 # 输出到 dist/
    python scripts/build-plugin-zip.py --out D:/build  # 指定输出目录
    python scripts/build-plugin-zip.py --check         # 只校验必备文件与版本，不打包
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_model_websearch"
REQUIRED_FILES = [
    "main.py",
    "client.py",
    "tools.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
]


def read_version() -> str:
    metadata = (REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*\"?v?([0-9]+\.[0-9]+\.[0-9]+)\"?\s*$", metadata, re.MULTILINE)
    if not match:
        raise SystemExit("metadata.yaml 里找不到 `version: vX.Y.Z`，无法确定打包版本。")
    return match.group(1)


def check_files() -> None:
    missing = [name for name in REQUIRED_FILES if not (REPO_ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"缺少必备文件：{', '.join(missing)}（插件目录：{REPO_ROOT}）")


def build(out_dir: Path, version: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PLUGIN_NAME}-v{version}.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in REQUIRED_FILES:
            source = REPO_ROOT / name
            archive.write(source, arcname=f"{PLUGIN_NAME}/{name}")
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 AstrBot 插件安装包 zip")
    parser.add_argument("--out", default=str(REPO_ROOT / "dist"), help="输出目录，默认 <repo>/dist")
    parser.add_argument("--check", action="store_true", help="只校验文件与版本，不生成 zip")
    args = parser.parse_args(argv)

    version = read_version()
    check_files()
    if args.check:
        print(f"校验通过：{PLUGIN_NAME} v{version}（{len(REQUIRED_FILES)} 个文件）")
        return 0

    target = build(Path(args.out), version)
    digest = sha256(target)
    print(f"插件包：{target}")
    print(f"版本：v{version}")
    print(f"文件数：{len(REQUIRED_FILES)}（含目录 {PLUGIN_NAME}/）")
    print(f"大小：{target.stat().st_size} 字节")
    print(f"sha256：{digest}")

    checksum_file = target.with_name(target.name + ".sha256.txt")
    checksum_file.write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"校验文件：{checksum_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
