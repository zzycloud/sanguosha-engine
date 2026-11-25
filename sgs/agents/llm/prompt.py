"""把玩家视角和合法动作渲染成提示词。

PromptBuilder 只读 PlayerView 和 Decision，所以看不到对手手牌（和智能体看到的一样）。
想换提示词格式（英文、更短、加牌面说明、给 SFT 数据用的固定模板），继承 PromptBuilder
改 system() / state() / decision() 中的一个即可，然后 register_prompt("名字") 并在配置里写 prompt=名字。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ...core.cards import rank_str

PROMPTS: Dict[str, Callable[..., "PromptBuilder"]] = {}


def register_prompt(name: str):
    def deco(cls):
        PROMPTS[name] = cls
        return cls
    return deco


PHASES = {"start": "回合开始", "prepare": "准备阶段", "judge": "判定阶段", "draw": "摸牌阶段",
          "play": "出牌阶段", "discard": "弃牌阶段", "finish": "结束阶段", "": "回合外"}
SLOTS = {"weapon": "武器", "armor": "防具", "plus_horse": "+1马", "minus_horse": "-1马"}
KINDS = {"play": "出牌", "respond": "响应（使用或打出一张牌）", "trigger": "是否发动技能", "choose": "选择"}

SYSTEM = """你在玩三国杀（标准版规则，不含军争），模式是 1v1 单挑：双方各一名武将，把对方体力打到 0 且无人救回就获胜。
每次轮到你做决定时，你会看到：你能看到的局面（看不到对手手牌和牌堆）、最近发生的事情、要做的决定和全部合法动作。
动作都已经过规则检查，你只需要从列表里选一个编号。"""

FORMAT_PLAIN = '只输出一行 JSON，不要输出别的内容：{"action": 编号}'
FORMAT_REASON = '只输出一行 JSON，不要输出别的内容：{"reason": "一两句理由", "action": 编号}'


def card_str(view, cid: int) -> str:
    try:
        c = view.card(cid)
    except KeyError:
        return "（看不到的牌）"
    return f"{c.suit}{rank_str(c.rank)}{c.name}"


@register_prompt("default")
class PromptBuilder:
    """history_lines：附上最近多少条可见日志（0 不附）；ask_reason：要求模型先写理由（做“讲理由”数据用）。
    system_prompt：替换开头的说明；extra_rules：追加在说明后面的文字（例如你自己的策略提示）。"""

    def __init__(self, history_lines: int = 40, ask_reason: bool = False, show_skill_text: bool = True,
                 system_prompt: Optional[str] = None, extra_rules: str = ""):
        self.history_lines = history_lines
        self.ask_reason = ask_reason
        self.show_skill_text = show_skill_text
        self.system_prompt = system_prompt
        self.extra_rules = extra_rules

    # ---------------------------------------------------------------- 组装
    def messages(self, view, d) -> List[Dict[str, str]]:
        parts = [self.state(view), self.history(view), self.decision(view, d)]
        return [{"role": "system", "content": self.system()},
                {"role": "user", "content": "\n\n".join(p for p in parts if p)}]

    def system(self) -> str:
        s = self.system_prompt or SYSTEM
        if self.extra_rules:
            s += "\n" + self.extra_rules
        return s + "\n" + (FORMAT_REASON if self.ask_reason else FORMAT_PLAIN)

    def retry(self, error: str, n: int) -> Dict[str, str]:
        """模型输出不合法时追加的一条用户消息。"""
        fmt = FORMAT_REASON if self.ask_reason else FORMAT_PLAIN
        return {"role": "user", "content": f"你的回答不合法：{error}。请从 0 到 {n - 1} 中选一个编号。{fmt}"}

    # ---------------------------------------------------------------- 各部分
    def player(self, view, p) -> List[str]:
        me = p.seat == view.viewer
        who = "你" if me else ("对手" if view.is_enemy(p.seat) else "队友")
        head = (f"【{who}】{p.seat} 号位 {p.general}（{p.kingdom}，{'男' if p.gender == 'male' else '女'}）"
                f" 体力 {p.hp}/{p.max_hp}" + ("" if p.alive else "（已阵亡）"))
        lines = [head]
        if p.skills:
            lines.append("  技能：" + "、".join(
                s + ("（锁定技）" if s in view.locked_skills else "") for s in p.skills))
        if p.hand is not None:
            lines.append(f"  手牌（{len(p.hand)}）：" + ("、".join(card_str(view, c) for c in p.hand) or "无"))
        else:
            lines.append(f"  手牌数：{p.hand_count}")
        eq = [f"{SLOTS.get(s, s)} {card_str(view, c)}" for s, c in p.equips.items()]
        lines.append("  装备：" + ("；".join(eq) or "无"))
        if p.judges:
            lines.append("  判定区：" + "、".join(f"{n}（{card_str(view, c)}）" for c, n in p.judges))
        if p.marks:
            lines.append("  标记：" + "、".join(f"{k}={v}" for k, v in p.marks.items()))
        return lines

    def state(self, view) -> str:
        cur = view.players[view.current] if view.current is not None else None
        lines = [f"第 {view.turn} 回合，当前回合角色：" + (f"{cur.seat} 号位 {cur.general}" if cur else "无")
                 + f"，{PHASES.get(view.phase, view.phase)}"]
        order = [view.viewer] + [p.seat for p in view.players if p.seat != view.viewer]
        for s in order:
            lines += self.player(view, view.players[s])
        lines.append(f"牌堆剩 {view.deck_count} 张，弃牌堆 {len(view.discard)} 张"
                     + (f"（最近：{'、'.join(card_str(view, c) for c in view.discard[-5:])}）" if view.discard else ""))
        if view.processing:
            lines.append("处理区：" + "、".join(card_str(view, c) for c in view.processing))
        if view.distance is not None:
            me = view.viewer
            rng = []
            for p in view.players:
                if p.seat != me and p.alive and (me, p.seat) in view.distance:
                    rng.append(f"你到 {p.seat} 号位距离 {view.distance[(me, p.seat)]}")
            lines.append("；".join(rng + [f"你的攻击范围 {view.attack_range.get(me)}",
                                          f"本回合已出杀 {view.slash_used} 次（上限 {view.slash_limit}）",
                                          f"手牌上限 {view.max_hand}"]))
        if self.show_skill_text and view.skill_text:
            lines.append("技能说明：")
            lines += [f"  {k}：{v}" for k, v in view.skill_text.items()]
        eq = getattr(view, "equip_text", None)
        if self.show_skill_text and eq:
            lines.append("装备说明：")
            lines += [f"  {k}：{v}" for k, v in eq.items()]
        return "【局面】\n" + "\n".join(lines)

    def history(self, view) -> str:
        if not self.history_lines or not view.logs:
            return ""
        logs = view.logs[-self.history_lines:]
        return "【最近发生的事】\n" + "\n".join(logs)

    def decision(self, view, d) -> str:
        opts = "\n".join(f"{i}. {o.label}" for i, o in enumerate(d.options))
        fmt = FORMAT_REASON if self.ask_reason else FORMAT_PLAIN
        return (f"【轮到你决定】（{KINDS.get(d.kind, d.kind)}）{d.prompt}\n"
                f"合法动作（共 {len(d.options)} 个）：\n{opts}\n\n{fmt}")


def make_prompt(name: str = "default", **kw: Any) -> PromptBuilder:
    if name in PROMPTS:
        return PROMPTS[name](**kw)
    if ":" in name:
        import importlib
        mod, _, attr = name.partition(":")
        return getattr(importlib.import_module(mod), attr)(**kw)
    raise KeyError(f"未知的提示词模板 {name!r}；已注册：{', '.join(sorted(PROMPTS))}")
