#!/usr/bin/env python3
"""离线单测入口：不安装 AstrBot 也能跑 tests/ 下的用例。

为什么需要它：插件的 client.py 只依赖 `astrbot.api.logger`，而 AstrBot 本身很重
（安装一份核心依赖才能跑 16 个纯逻辑用例不值得）。这个脚本在导入被测模块之前，往
`sys.modules` 里放一个只含 logger 的最小 `astrbot.api` stub；stub 只影响本进程，不会
写入磁盘、不会改动真实安装。

用法：
    python scripts/run-offline-tests.py            # 详细输出
    python scripts/run-offline-tests.py --quiet    # 只输出结果
"""

from __future__ import annotations

import argparse
import logging
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"


def install_astrbot_stub() -> None:
    """只补 `astrbot.api.logger`；已安装真实 AstrBot 时不覆盖。"""
    if "astrbot" in sys.modules:
        return
    package = types.ModuleType("astrbot")
    package.__path__ = []  # type: ignore[attr-defined]  # 让 astrbot.* 的子模块查找可控
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")  # type: ignore[attr-defined]
    package.api = api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = package
    sys.modules["astrbot.api"] = api


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑插件离线单测（AstrBot 用最小 stub 替代）")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总，不打印每条用例")
    args = parser.parse_args(argv)

    install_astrbot_stub()
    # 允许用例 `from client import ...`：仓库根目录就是插件目录本身。
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    if not TESTS_DIR.is_dir():
        print(f"找不到测试目录：{TESTS_DIR}", file=sys.stderr)
        return 1

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS_DIR),
        pattern="test_*.py",
        top_level_dir=str(TESTS_DIR),
    )
    verbosity = 1 if args.quiet else 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    total = result.testsRun
    print(f"\n合计 {total} 项：失败 {len(result.failures)}，错误 {len(result.errors)}，跳过 {len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
