"""本地 AstrBot 实例的生命周期管理。

约定：

- **AstrBot 源码**：用 ``ASTRBOT_SRC`` 指定，或放在仓库同级目录 ``../AstrBot``。
  只读使用，绝不修改（它的 ``data/`` 指向我们的运行目录）。
- **运行目录**：仓库下 ``.runtime/``，用 ``ASTRBOT_ROOT`` 环境变量指定，
  于是 ``data/``、``plugin_data/`` 全部落在 ``.runtime/data`` 里，
  不污染用户那份源码 checkout。
- **插件接入**：在 ``.runtime/data/plugins/`` 建一个目录联接（junction）指向
  本仓库根目录，名字用合法模块名 ``astrbot_plugin_super_bot``。
  改本地代码即生效，无需复制。

用法（在 pytest 里）::

    async with AstrBotInstance() as env:
        bot = FakeOneBot(port=env.onebot_port)
        await bot.connect()
        ...
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["AstrBotInstance", "PLUGIN_MODULE_NAME", "REPO_ROOT", "RUNTIME_DIR"]


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / ".runtime"
DATA_DIR = RUNTIME_DIR / "data"
PLUGINS_DIR = DATA_DIR / "plugins"
LOGS_DIR = RUNTIME_DIR / "logs"

PLUGIN_MODULE_NAME = "astrbot_plugin_super_bot"
"""插件在 data/plugins 下的目录名，必须是合法模块名。"""


def astrbot_src() -> Path:
    """AstrBot 源码位置。

    优先 ``ASTRBOT_SRC``；否则在仓库旁边找几个常见位置。**不写死任何人的机器路径**
    —— 那是本地环境，不该进仓库。
    """
    raw = os.environ.get("ASTRBOT_SRC")
    if raw:
        return Path(raw).resolve()

    for cand in (
        REPO_ROOT.parent / "AstrBot",
        REPO_ROOT / "AstrBot",
        Path.home() / "AstrBot",
        Path.home() / "astrbot",
    ):
        if (cand / "astrbot").is_dir():
            return cand.resolve()

    raise RuntimeError(
        "找不到 AstrBot 源码。请设置环境变量 ASTRBOT_SRC 指向 AstrBot 仓库根目录"
        "（里面有 astrbot/ 和 main.py）。"
    )


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

FAKE_LLM_PORT = 18180
ONEBOT_PORT = 16199
DASHBOARD_PORT = 16185
"""测试实例用的端口**故意避开 AstrBot 的默认值**（8180/6199/6185）。

为什么：默认端口是"真实例"会用的。测试如果也占默认端口，那么这台机器上
一旦跑着真实服务，测试就会和它抢端口——轻则测试失败、重则把真实例顶掉。
错开之后两者可以共存，也顺便让"端口被占"这个报错只可能来自我们自己的残留进程。
"""


def build_config(
    *,
    onebot_port: int = ONEBOT_PORT,
    llm_port: int = FAKE_LLM_PORT,
    admins: list[str] | None = None,
    unique_session: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """最小配置；缺失项由 AstrBot 的 check_config_integrity 自动补齐。

    依据：
    - aiocqhttp 平台模板见 ``astrbot/core/config/default.py:345-352``
    - openai 兼容 provider 模板见 ``:1294-1305``
    - ``model`` 是顶层字段（``utils/migra_helper.py:403-406`` 会把旧的
      ``model_config.model`` 迁移过来）
    """
    cfg: dict[str, Any] = {
        "platform": [
            {
                "id": "onebot-local",
                "type": "aiocqhttp",
                "enable": True,
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": onebot_port,
                "ws_reverse_token": "",
            }
        ],
        "provider": [
            {
                "id": "fake-llm",
                "provider": "openai",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "enable": True,
                "key": ["sk-fake"],
                "api_base": f"http://127.0.0.1:{llm_port}/v1",
                "timeout": 60,
                "proxy": "",
                "custom_headers": {},
                "model": "fake-model",
            }
        ],
        "provider_settings": {
            "enable": True,
            "provider_pool": ["*"],
            "streaming_response": False,
            "computer_use_runtime": "none",
            "reachability_check": False,
        },
        "wake_prefix": ["/"],
        "admins_id": admins if admins is not None else ["20001"],
        "plugin_set": ["*"],
        "platform_settings": {"unique_session": unique_session},
        # 测试实例的 Dashboard 也换非默认端口，避免和真实例抢 6185
        "dashboard": {
            "enable": True,
            "host": "127.0.0.1",
            "port": DASHBOARD_PORT,
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


def write_config(cfg: dict[str, Any]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "cmd_config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
    return path


def write_plugin_config(cfg: dict[str, Any]) -> Path:
    """写插件自己的配置（Dashboard 里那张表单存的地方）。

    路径是 ``data/config/<插件目录名>_config.json``（``star_manager.py:206,1164-1169``），
    结构由插件根目录的 ``_conf_schema.json`` 决定。
    """
    path = RUNTIME_DIR / "data" / "config" / f"{PLUGIN_MODULE_NAME}_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_plugin_config() -> None:
    """删掉插件配置，回到"AstrBot 按 schema 生成默认值"的状态。"""
    path = RUNTIME_DIR / "data" / "config" / f"{PLUGIN_MODULE_NAME}_config.json"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# 真实模型（复用本机已有的凭据）
# ---------------------------------------------------------------------------

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"


def _read_dsh_secret(name: str) -> str | None:
    """从 DSH 的凭据文件里取一个密钥（不打印、不落盘）。"""
    if not DSH_CREDENTIALS.exists():
        return None
    import re

    text = DSH_CREDENTIALS.read_text(encoding="utf-8")
    m = re.search(rf"{re.escape(name)}\s*:\s*(\S+)", text)
    return m.group(1) if m else None


def deepseek_provider(model: str = "deepseek-flash") -> dict[str, Any] | None:
    """构造一个指向 DeepSeek 官方 API 的 provider 条目。

    凭据复用 ``~/.dsh/.credentials.yaml`` 里的 ``DEEPSEEK_API_KEY``
    ——也就是当前 DSH 会话所用的那把钥匙，只是换成官方端点。

    Returns:
        provider 字典；拿不到 key 时返回 ``None``。
    """
    key = _read_dsh_secret("DEEPSEEK_API_KEY")
    if not key:
        return None
    return {
        "id": "deepseek-live",
        "provider": "openai",
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
        "key": [key],
        "api_base": "https://api.deepseek.com/v1",
        "timeout": 120,
        "proxy": "",
        "custom_headers": {},
        "model": model,
    }


def config_with_real_model(
    *,
    model: str = "deepseek-flash",
    onebot_port: int = ONEBOT_PORT,
) -> dict[str, Any]:
    """用真实模型替换假模型的配置。"""
    cfg = build_config(onebot_port=onebot_port, llm_port=FAKE_LLM_PORT)
    provider = deepseek_provider(model)
    if provider is None:
        raise RuntimeError("拿不到 DEEPSEEK_API_KEY，无法跑真实模型用例")
    cfg["provider"] = [provider]
    return cfg


# ---------------------------------------------------------------------------
# 插件接入
# ---------------------------------------------------------------------------


def ensure_plugin_link() -> Path:
    """把仓库根目录联接到 ``data/plugins/astrbot_plugin_super_bot``。"""
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    link = PLUGINS_DIR / PLUGIN_MODULE_NAME
    if link.exists() or link.is_symlink():
        # 已存在就删掉重建，确保指向正确
        if link.is_dir() and not link.is_symlink():
            try:
                link.rmdir()
            except OSError:
                # junction 需要特殊删除
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(link)], check=False, capture_output=True
                )
    if not link.exists():
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(REPO_ROOT)],
            check=True,
            capture_output=True,
        )
    return link


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@dataclass
class AstrBotInstance:
    """一个跑在子进程里的 AstrBot。"""

    onebot_port: int = ONEBOT_PORT
    llm_port: int = FAKE_LLM_PORT
    config: dict[str, Any] | None = None
    boot_timeout: float = 90.0
    extra_env: dict[str, str] = field(default_factory=dict)
    """额外环境变量（例如 ``SUPERBOT_EVAL_MODE=echo`` 切桩评估器行为）。"""

    proc: subprocess.Popen | None = None
    log_path: Path = field(default=RUNTIME_DIR / "logs" / "astrbot.log")

    # -- 上下文管理 -------------------------------------------------------

    async def __aenter__(self) -> "AstrBotInstance":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- 启动 / 停止 -------------------------------------------------------

    async def start(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS_DIR / "astrbot.log"
        write_config(
            self.config
            if self.config is not None
            else build_config(onebot_port=self.onebot_port, llm_port=self.llm_port)
        )
        ensure_plugin_link()

        # 上一轮泄漏的进程会占着这两个端口，症状很隐蔽（新实例启动到一半退出，
        # 日志里却是"配置没生效"）。所以先查，查到了就明确报错。
        for port, label in (
            (self.onebot_port, "OneBot"),
            (DASHBOARD_PORT, "Dashboard"),
        ):
            if _port_open("127.0.0.1", port):
                raise RuntimeError(
                    f"端口 {port}（{label}）已被占用，多半是上一轮 e2e 的残留进程。"
                    f"先把它关掉再跑：Get-CimInstance Win32_Process | "
                    f"Where-Object {{ $_.CommandLine -like '*AstrBot*main.py*' }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
                )

        env = os.environ.copy()
        env["ASTRBOT_ROOT"] = str(RUNTIME_DIR)
        env["PYTHONPATH"] = str(astrbot_src())
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.extra_env)

        self._log_fp = open(self.log_path, "w", encoding="utf-8", buffering=1)
        self.proc = subprocess.Popen(
            [sys.executable, str(astrbot_src() / "main.py")],
            cwd=str(RUNTIME_DIR),
            env=env,
            stdout=self._log_fp,
            stderr=subprocess.STDOUT,
        )
        await self.wait_ready()

    async def wait_ready(self, timeout: float | None = None) -> None:
        """等到 OneBot 端口可连接，或日志出现适配器已连接。"""
        deadline = time.monotonic() + (timeout or self.boot_timeout)
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"AstrBot 提前退出（code={self.proc.returncode}）。\n--- 日志尾部 ---\n"
                    + self.log_tail(60)
                )
            if _port_open("127.0.0.1", self.onebot_port):
                # 端口开了说明 aiocqhttp 已起；再给它一点时间加载插件
                await asyncio.sleep(1.5)
                return
            if "适配器已连接" in self.log_tail(200):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            "AstrBot 未在预期时间内就绪。\n--- 日志尾部 ---\n" + self.log_tail(80)
        )

    async def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                await asyncio.to_thread(self.proc.wait, 10)
        self.proc = None
        fp = getattr(self, "_log_fp", None)
        if fp is not None:
            fp.close()
            self._log_fp = None

    # -- 日志 -------------------------------------------------------------

    def log_text(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def log_tail(self, lines: int = 40) -> str:
        text = self.log_text()
        if not text:
            return "(日志为空)"
        return "\n".join(text.splitlines()[-lines:])

    async def wait_log(self, needle: str, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.log_text():
                return True
            await asyncio.sleep(0.3)
        return False
