"""pytest 引导：把仓库根目录放进 sys.path。

这样 ``from core.gate import ...`` 在未安装本包的情况下也能工作
（插件在 AstrBot 里是 ``data.plugins.<name>.main``，本地开发时用根目录导入）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
