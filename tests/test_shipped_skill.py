"""插件自带技能的"别让它烂掉"测试。

技能是我们写给模型的行为说明书，改坏了不会报错、只会让它变笨，
所以必须有一条测试盯着：能不能被发现、description 写没写、
关键内容还在不在。
"""

from __future__ import annotations

from pathlib import Path

from superbot.skills import Skills

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_SKILLS = PLUGIN_ROOT / "skills"


def _load() -> Skills:
    lib = Skills([SHIPPED_SKILLS])
    lib.load()
    return lib


def test_shipped_skill_is_discoverable():
    skills = _load().list()
    assert skills, "插件没带任何技能，或者 skills/ 目录结构不对"
    names = {s.name for s in skills}
    assert "super-bot" in names, f"没扫到 super-bot 技能：{names}"


def test_shipped_skill_has_a_usable_description():
    """description 是模型在清单里唯一能看到的东西，含糊就等于不存在。"""
    skill = _load().get("super-bot")
    assert skill is not None
    desc = skill.description
    assert "superbot_dispatch" in desc, "说明里得点出工具名，模型才知道调什么"
    assert "不要用" in desc or "不要" in desc, "得说清什么时候**别**用"
    assert 20 <= len(desc) <= 200, f"说明长度不合适（{len(desc)}）"


def test_shipped_skill_states_the_hard_rules():
    text = _load().read("super-bot")
    assert "不许编造" in text, "少了『不许编造结果』这条底线"
    assert "/sb do" in text, "得告诉模型用户还有另一个入口"


def test_shipped_skill_names_the_three_work_types():
    """重活 / 脏活 / 长活 —— 这三类必须点名，否则模型只认"算数"。"""
    text = _load().read("super-bot")
    for word in ("重活", "脏活", "长活"):
        assert word in text, f"没写清楚「{word}」该派"
    assert "superbot_dispatch" in text


def test_shipped_skill_tells_model_not_to_retry_network_failures():
    """11 月 2 日那天：沙箱里 curl 失败 → 查 DNS → 换镜像，白烧 45 秒。

    边界本身是环境相关的（能上网，但某些站点被封），所以要写死的是**行为**：
    试一次，不通就停，并且如实说"访问不了"。
    """
    text = _load().read("super-bot")
    assert "只试一次" in text, "没写'只试一次'，模型会反复重试"
    assert "别换镜像" in text or "别改 DNS" in text
    assert "访问不了" in text, "得教它如实说'访问不了'，而不是'没找到'"
