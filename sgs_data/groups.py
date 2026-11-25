"""把“其实是同一个选择”的选项并成一组。

例如手里两张闪，“使用 ♥2闪”和“使用 ♦J闪”对结局没有区别；弃牌时弃 ♠7杀 还是 ♣9杀 也一样。
不合并的话，模拟会把预算浪费在比较两张一样的牌上，还会产生大量“平局”题。

合并规则：同一种动作、同一个牌名、同一种颜色、同样的转化技能和目标算一组。
颜色要分开，因为甄姬的黑牌能当闪、黑杀打不穿仁王盾。观星、鬼才这类看点数花色的决定不合并。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

NO_MERGE_SKILLS = {"观星", "鬼才"}
COLOR = {"black": "黑", "red": "红", None: ""}


def _cards_color(g, cids) -> Any:
    cols = {g.cards[c].color for c in cids}
    return cols.pop() if len(cols) == 1 else ("mixed" if cols else None)


def option_desc(g, d, i: int) -> Tuple[Tuple, str]:
    """(合并用的键, 给人看的简短描述)。g 可以是 Game，也可以是带 cards 的对象。"""
    o = d.options[i]
    k = o.key
    ctx = d.ctx or {}
    if ctx.get("skill") in NO_MERGE_SKILLS:
        return ("single", i), o.label
    kind = k[0] if k else "?"
    if kind in ("use", "card") and len(k) >= 5 and isinstance(k[2], tuple):
        name, subs, skill, targets = k[1], k[2], k[3], k[4]
        col = _cards_color(g, subs)
        real = [g.cards[c].name for c in subs]
        converted = bool(skill) or real != [name]
        verb = "使用" if kind == "use" else "出"
        col_s = COLOR.get(col, "") if (name == "杀" or converted) else ""
        desc = f"{verb}{col_s}{name}"
        if converted:
            desc += f"（{skill or '转化'}：{''.join(real)}）"
        if targets:
            desc += "→" + "、".join("自己" if t == d.player else "对手" for t in targets)
        return (kind, name, col, skill, tuple(real) if converted else (), tuple(targets)), desc
    if kind == "card" and len(k) == 2 and isinstance(k[1], int):
        c = g.cards[k[1]]
        reason = ctx.get("reason", "")
        if "target" in ctx:                      # 选对方区域里的牌（反馈、拆、顺）
            return ("card", k[1]), o.label
        col = COLOR.get(c.color, "")
        name = c.name if c.name != "杀" else f"{col}杀"
        prefix = "弃" if "discard" in reason else ""
        return ("card", c.name, c.color), f"{prefix}{name}"
    if kind == "give":
        return tuple(k), "自己留下" if k[1] == d.player else "交给对手"
    if kind == "end":
        return ("end",), "结束出牌阶段"
    if kind == "pass":
        return ("pass",), "不出"
    if kind in ("yes", "no"):
        return (kind,), "发动" if kind == "yes" else "不发动"
    if kind == "hand":
        return ("hand",), "拿一张手牌"
    if kind == "skill":
        return tuple(k), f"发动【{k[1]}】"
    if kind == "stop":
        return ("stop",), o.label
    return ("single", i), o.label


def group_options(g, d) -> Tuple[List[List[int]], List[str]]:
    """返回 (每组的选项下标, 每组的描述)。每组第一个下标是代表。"""
    order: List[Tuple] = []
    members: Dict[Tuple, List[int]] = {}
    descs: Dict[Tuple, str] = {}
    for i in range(len(d.options)):
        key, desc = option_desc(g, d, i)
        if key not in members:
            order.append(key)
            members[key] = []
            descs[key] = desc
        members[key].append(i)
    groups = [members[k] for k in order]
    labels = [descs[k] for k in order]
    # 描述重复时补上第一个选项的原文，免得看不出区别
    seen: Dict[str, int] = {}
    for lb in labels:
        seen[lb] = seen.get(lb, 0) + 1
    labels = [lb if seen[lb] == 1 else f"{lb}（{d.options[grp[0]].label}）" for lb, grp in zip(labels, groups)]
    return groups, labels


def group_of(groups: List[List[int]], i: int) -> int:
    for j, grp in enumerate(groups):
        if i in grp:
            return j
    return -1
