"""技能（``SKILL.md``）的发现、解析与读取。

为什么不直接用框架那套：AstrBot 的 ``build_skills_prompt``
（``astrbot/core/skills/skill_manager.py:212``）教模型用 **shell** 去读
``SKILL.md``；而 ``computer_use_runtime=sandbox`` 时，sh 是在沙箱里跑的
（``astr_main_agent.py:1082`` 起那一段），读一个技能就得先起一个沙箱。

这里的做法是：**直接从宿主读**，把正文当工具返回值交给模型。不依赖 shell、
不依赖沙箱、也不依赖 ``computer_use_runtime`` 开没开。

目录约定跟框架保持一致（这样 Dashboard 里能看见、能开关、别人也认）::

    data/skills/<name>/SKILL.md              # 全局技能
    data/plugins/<插件>/skills/<name>/SKILL.md  # 插件自带技能

``data/skills.json`` 里的 ``{"skills": {"<name>": {"active": false}}}`` 用来关。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Skill", "Skills", "SKILL_NAME_RE"]

SKILL_NAME_RE = re.compile(r"^[\w.-]+$")
"""和框架一致（``skill_manager.py:34``）：名字只能是字母数字下划线点横线。"""

FRONTMATTER_DESC_RE = re.compile(r"^\s*description\s*:\s*(.+)$", re.MULTILINE)
FRONTMATTER_NAME_RE = re.compile(r"^\s*name\s*:\s*(.+)$", re.MULTILINE)

MAX_SKILL_BYTES = 64 * 1024
"""单个技能文件最多读这么多，防止有人塞个几兆的 PDF 进来。"""


@dataclass(frozen=True)
class Skill:
    """一个技能。"""

    name: str
    description: str
    path: Path
    """``SKILL.md`` 的绝对路径。"""
    root: Path
    """技能目录，读 ``scripts/`` 之类的相对文件时用。"""
    source: str = "local"

    def file_list(self, limit: int = 40) -> list[str]:
        """技能目录下的文件（相对路径），供模型决定要不要取。"""
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if rel == "SKILL.md":
                continue
            out.append(rel)
            if len(out) >= limit:
                break
        return out


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """解析 ``---`` 之间的 YAML 头（只认扁平的 ``key: value``）。

    不引 yaml 依赖：技能头就几个字段，多一个依赖对插件是负担。
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _strip_quotes(value.strip())
    return meta


def first_heading(text: str) -> str:
    """没有 frontmatter 说明时，退而取正文第一个非空行当说明。"""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if line:
            return line[:120]
    return ""


class Skills:
    """技能库：扫描、过滤、读取。"""

    def __init__(
        self,
        roots: list[Path],
        config_path: Path | None = None,
        max_bytes: int = MAX_SKILL_BYTES,
    ) -> None:
        self.roots = [Path(r) for r in roots]
        self.config_path = Path(config_path) if config_path else None
        self.max_bytes = max_bytes
        self._skills: dict[str, Skill] = {}
        self._alias: dict[str, str] = {}
        self._problems: list[str] = []

    # -- 发现 -------------------------------------------------------------

    def _active_map(self) -> dict[str, bool]:
        if self.config_path is None or not self.config_path.exists():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return {}
        table = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            return {}
        out: dict[str, bool] = {}
        for name, cfg in table.items():
            if isinstance(cfg, dict):
                out[str(name)] = bool(cfg.get("active", True))
        return out

    def load(self) -> list[Skill]:
        """扫一遍所有根目录，返回**可用**的技能（已按 active 过滤）。"""
        self._skills = {}
        self._alias = {}
        self._problems = []
        active = self._active_map()

        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_dir in sorted(root.iterdir(), key=lambda p: p.name):
                if not skill_dir.is_dir():
                    continue
                name = skill_dir.name
                if not SKILL_NAME_RE.match(name):
                    self._problems.append(f"{name}: 名字不合法（只允许字母数字 _ . -）")
                    continue
                md = skill_dir / "SKILL.md"
                if not md.is_file():
                    continue
                if not active.get(name, True):
                    continue
                try:
                    text = md.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001
                    self._problems.append(f"{name}: 读不了 SKILL.md（{exc}）")
                    continue
                meta = parse_frontmatter(text)
                desc = (meta.get("description") or "").strip() or first_heading(text)
                if not desc:
                    self._problems.append(f"{name}: 没有 description，模型不知道何时用它")
                    desc = "（没有说明）"
                self._skills[name] = skill = Skill(
                    name=meta.get("name") or name,
                    description=desc[:200],
                    path=md,
                    root=skill_dir,
                )
                # 模型看到的是 frontmatter 里的 name，读的时候可能照抄它；
                # 目录名和它不一致时两个都得认，否则"清单里有、读不到"
                self._alias[name] = name
                if skill.name != name:
                    self._alias.setdefault(skill.name, name)
        return self.list()

    def list(self) -> list[Skill]:
        return [self._skills[k] for k in sorted(self._skills)]

    @property
    def problems(self) -> list[str]:
        """扫描时发现的问题（启动时打 WARN 用）。"""
        return list(self._problems)

    def get(self, name: str) -> Skill | None:
        key = name.strip().strip("/")
        if key in self._alias:
            return self._skills.get(self._alias[key])
        if key in self._skills:
            return self._skills[key]
        # 容忍模型写成 "skills/xxx/SKILL.md"、"xxx/SKILL.md"、"skills/xxx"
        for part in [p for p in key.split("/") if p]:
            if part in self._alias:
                return self._skills.get(self._alias[part])
            if part in self._skills:
                return self._skills[part]
        return None

    # -- 渲染 -------------------------------------------------------------

    def index_text(self) -> str:
        """给 prompt 用的技能清单（只有名字和一句话说明）。"""
        if not self._skills:
            return ""
        lines = ["## 可用技能", ""]
        for s in self.list():
            lines.append(f"- {s.name}: {s.description}")
        lines.append("")
        lines.append("需要完整说明时调用 `read_skill` 工具（不用开沙箱）。")
        return "\n".join(lines)

    def read(self, name: str, file: str | None = None) -> str:
        """读技能正文，或技能目录里的某个文件。"""
        skill = self.get(name)
        if skill is None:
            known = "、".join(s.name for s in self.list()) or "（一个都没有）"
            return f"没有这个技能：{name}。现有技能：{known}"

        if not file or file in ("SKILL.md", "./SKILL.md"):
            target = skill.path
        else:
            rel = file.strip().lstrip("/")
            if ".." in Path(rel).parts:
                return "不允许读技能目录之外的文件。"
            target = skill.root / rel
            try:
                target.resolve().relative_to(skill.root.resolve())
            except ValueError:
                return "不允许读技能目录之外的文件。"
            if not target.is_file():
                files = skill.file_list() or ["（没有其它文件）"]
                return f"技能 {skill.name} 里没有 {rel}。目录里有：\n" + "\n".join(
                    f"- {f}" for f in files
                )

        size = target.stat().st_size
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text.encode("utf-8")) > self.max_bytes:
            text = text[: self.max_bytes // 4] + "\n\n…（文件太大，已截断）"
        head = f"# 技能 {skill.name} / {target.relative_to(skill.root).as_posix()}（{size} 字节）\n\n"
        body = head + text
        # 只有正文时，顺手把目录里的其它文件报出来，模型才知道有脚本可拿
        if target == skill.path:
            extra = skill.file_list()
            if extra:
                body += "\n\n---\n这个技能目录里还有：\n" + "\n".join(f"- {f}" for f in extra)
                body += "\n（用 read_skill 的 file 参数取具体文件）"
        return body
