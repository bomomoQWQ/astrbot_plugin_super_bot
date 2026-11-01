"""技能发现与读取（``superbot/skills.py``）。

重点是"可靠"：坏技能要在加载时报出来，读文件不能越界，
模型把名字写歪了也要能兜住。
"""

from __future__ import annotations

import json

import pytest

from superbot.skills import Skills, parse_frontmatter


def _skill(root, name: str, body: str, desc: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    head = ""
    if desc is not None:
        head = f"---\nname: {name}\ndescription: {desc}\n---\n\n"
    (d / "SKILL.md").write_text(head + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_basic():
    meta = parse_frontmatter("---\nname: x\ndescription: 什么时候用\n---\nbody")
    assert meta["name"] == "x"
    assert meta["description"] == "什么时候用"


def test_parse_frontmatter_quoted_and_missing():
    assert parse_frontmatter('---\ndescription: "带引号"\n---\n')["description"] == "带引号"
    assert parse_frontmatter("没有头") == {}
    assert parse_frontmatter("---\nname: x\n") == {}  # 没闭合


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def test_load_parses_description(tmp_path):
    _skill(tmp_path, "alpha", "正文", desc="什么时候用 alpha")
    lib = Skills([tmp_path])
    skills = lib.load()
    assert [s.name for s in skills] == ["alpha"]
    assert skills[0].description == "什么时候用 alpha"


def test_load_falls_back_to_first_heading(tmp_path):
    _skill(tmp_path, "beta", "# 用 beta 干正事\n\n细节")
    skills = Skills([tmp_path]).load()
    assert skills[0].description == "用 beta 干正事"


def test_load_respects_active_flag(tmp_path):
    _skill(tmp_path, "on", "x", desc="开")
    _skill(tmp_path, "off", "x", desc="关")
    cfg = tmp_path / "skills.json"
    cfg.write_text(json.dumps({"skills": {"off": {"active": False}}}), encoding="utf-8")

    names = [s.name for s in Skills([tmp_path], config_path=cfg).load()]
    assert names == ["on"]


def test_load_skips_bad_names_and_missing_md(tmp_path):
    _skill(tmp_path, "good", "x", desc="好")
    (tmp_path / "带空格 的名字").mkdir()
    (tmp_path / "带空格 的名字" / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / "empty_dir").mkdir()

    lib = Skills([tmp_path])
    assert [s.name for s in lib.load()] == ["good"]
    assert any("名字不合法" in p for p in lib.problems)


def test_load_reports_missing_description(tmp_path):
    _skill(tmp_path, "nodesc", "---\nname: nodesc\n---\n\n")
    lib = Skills([tmp_path])
    lib.load()
    assert any("没有 description" in p for p in lib.problems)


def test_load_merges_multiple_roots(tmp_path):
    a = tmp_path / "global"
    b = tmp_path / "plugin" / "skills"
    a.mkdir()
    b.mkdir(parents=True)
    _skill(a, "one", "x", desc="一")
    _skill(b, "two", "x", desc="二")
    assert [s.name for s in Skills([a, b]).load()] == ["one", "two"]


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def test_read_returns_body(tmp_path):
    _skill(tmp_path, "alpha", "具体步骤 1、2、3", desc="说明")
    lib = Skills([tmp_path])
    lib.load()
    text = lib.read("alpha")
    assert "具体步骤 1、2、3" in text
    assert "技能 alpha" in text


def test_read_tolerates_sloppy_name(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="说明")
    lib = Skills([tmp_path])
    lib.load()
    assert "技能 alpha" in lib.read("skills/alpha/SKILL.md")


def test_read_unknown_lists_available(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="说明")
    lib = Skills([tmp_path])
    lib.load()
    out = lib.read("nope")
    assert "没有这个技能" in out
    assert "alpha" in out


def test_read_sub_file(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="说明")
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print(1)", encoding="utf-8")

    lib = Skills([tmp_path])
    lib.load()
    assert "print(1)" in lib.read("alpha", "scripts/run.py")
    assert "scripts/run.py" in lib.read("alpha")


def test_read_rejects_path_traversal(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="说明")
    (tmp_path / "secret.txt").write_text("机密", encoding="utf-8")
    lib = Skills([tmp_path])
    lib.load()
    out = lib.read("alpha", "../../secret.txt")
    assert "不允许" in out
    assert "机密" not in out


def test_read_missing_file_lists_dir(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="说明")
    (tmp_path / "alpha" / "notes.md").write_text("n", encoding="utf-8")
    lib = Skills([tmp_path])
    lib.load()
    out = lib.read("alpha", "nope.md")
    assert "没有 nope.md" in out
    assert "notes.md" in out


def test_read_truncates_huge_file(tmp_path):
    _skill(tmp_path, "alpha", "x" * 5000, desc="说明")
    lib = Skills([tmp_path], max_bytes=1000)
    lib.load()
    assert "已截断" in lib.read("alpha")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_index_text_lists_skills(tmp_path):
    _skill(tmp_path, "alpha", "x", desc="做甲")
    _skill(tmp_path, "beta", "x", desc="做乙")
    lib = Skills([tmp_path])
    lib.load()
    text = lib.index_text()
    assert "alpha: 做甲" in text
    assert "beta: 做乙" in text
    assert "read_skill" in text


def test_index_text_empty_when_no_skills(tmp_path):
    assert Skills([tmp_path]).load() == []
    assert Skills([tmp_path]).index_text() == ""
