#!/usr/bin/env python3
"""隔离模式入口：在 python3 -I 下也能从仓库根运行分析。

常规用法仍为 `python3 -m analysis_app ...`（见 README）；本脚本只把仓库
根显式加入 sys.path，使 -I（忽略 cwd 与 PYTHONPATH）下可复现执行。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis_app.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
