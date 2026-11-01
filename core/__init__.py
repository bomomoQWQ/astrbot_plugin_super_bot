"""Super Bot 插件内部实现。

分层约定（见 docs/设计/20-p0-spec.md）：
- 本包内的纯逻辑模块（gate / thread_key / limits）**不得 import astrbot**，
  以便在无 AstrBot 环境（例如本地开发机）中单测。
- 只有 main.py 与其适配层允许触碰 astrbot API。
"""

__all__ = ["gate", "limits", "thread_key"]
