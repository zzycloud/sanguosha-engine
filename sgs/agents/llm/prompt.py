"""把玩家视角和合法动作渲染成提示词。

PromptBuilder 只读 PlayerView 和 Decision，所以看不到对手手牌（和智能体看到的一样）。
想换提示词格式（英文、更短、加牌面说明、给 SFT 数据用的固定模板），继承 PromptBuilder
改 system() / state() / decision() 中的一个即可，然后 register_prompt("名字") 并在配置里写 prompt=名字。

内置两个模板：
  default  原来的写法：局面 + 最近 history_lines 条可见日志（含“进入出牌阶段”这类流程记录）
  v2       局面里加“已知手牌”（双方各知道对方手里哪些牌、怎么知道的），近况只放本回合和上一回合的
           关键事件（出牌、响应、伤害、回复、获得/失去牌、技能、判定），去掉流程记录
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
                                          f"本回合已出杀 {view.slash_used} 次（上限 "
                                          f"{'不限' if (view.slash_limit or 0) >= 1000 else view.slash_limit}）",
                                          f"手牌上限 {view.max_hand}"]))
        if self.show_skill_text and view.skill_text:
            lines.append("技能说明：")
            lines += [f"  {k}：{v}" for k, v in view.skill_text.items()]
        eq = self.equip_notes(view)
        if self.show_skill_text and eq:
            lines.append("装备说明：")
            lines += [f"  {k}：{v}" for k, v in eq.items()]
        return "【局面】\n" + "\n".join(lines)

    def equip_notes(self, view) -> Dict[str, str]:
        """要附上说明的装备（牌名 -> 效果）。默认是视角里看得到的全部装备牌，包括弃牌堆里的。"""
        return getattr(view, "equip_text", None) or {}

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


# ---------------------------------------------------------------------------- v2

SYSTEM_V2 = """你在玩三国杀（标准版规则，不含军争），模式是 1v1 单挑：双方各一名武将，把对方体力打到 0 且对方没被救回就获胜。
每次轮到你做决定时，你会看到：
- 局面：双方武将、体力、技能原文、装备、判定区、距离，你的手牌；对手的手牌你看不到，但如果你确实知道其中某几张
  （例如五谷丰登时对方拿走的、你被顺手牵羊拿走的、对方仁德交给你又被你看到的），会列出来；对手知道你手里哪些牌也会列出来；
- 最近发生的事：本回合和上一回合的关键事件；
- 要做的决定和全部合法动作。
动作都已经过规则检查，你只需要从列表里选一个编号：选让你最终最可能获胜的那个。"""

# 流程记录：进入/结束某阶段、回合开始分隔线、开局介绍、洗牌。v2 的近况里不放
FLOW_KINDS = {"phase", "turn", "setup", "shuffle"}


def known_note(view, cid: int, holder: int) -> str:
    """一张已知手牌是怎么知道的，例如“第 3 回合五谷丰登”“第 4 回合顺手牵羊，从你的手牌”。"""
    k = view.known_info.get(cid)
    if not k:
        return ""
    who = lambda s: "你" if s == view.viewer else ("对手" if view.is_enemy(s) else f"{s} 号位")  # noqa: E731
    s = f"第 {k['turn']} 回合{k['how']}"
    src = k.get("from")
    if k.get("zone") == "h" and src is not None and src != holder:
        s += f"，原来在{who(src)}手里"
    elif k.get("zone") == "e" and src is not None:
        s += f"，原来在{who(src)}的装备区"
    elif k.get("zone") == "j" and src is not None:
        s += f"，原来在{who(src)}的判定区"
    elif k.get("zone") in ("processing", "discard"):
        s += "，亮出过"
    return s


@register_prompt("v2")
class PromptV2(PromptBuilder):
    """recent_turns：近况放最近几个回合（默认 2：本回合和上一回合）；写 0 就退回旧方式（最近 history_lines 条日志）。
    keep_flow：近况里保留“进入出牌阶段”之类的流程记录（默认不保留）。max_events：近况最多多少条（太多时留最新的）。"""

    def __init__(self, recent_turns: int = 2, keep_flow: bool = False, max_events: int = 60, **kw):
        kw.setdefault("history_lines", 40)
        super().__init__(**kw)
        self.recent_turns = recent_turns
        self.keep_flow = keep_flow
        self.max_events = max_events

    def system(self) -> str:
        if not self.system_prompt:
            self.system_prompt = SYSTEM_V2
        return super().system()

    def equip_notes(self, view) -> Dict[str, str]:
        """只说明场上、你手里和对手已知手牌里的装备（弃牌堆里的不说明）。"""
        names = set()
        for p in view.players:
            names.update(view.card(c).name for c in p.equips.values())
            names.update(view.card(c).name for c in (p.hand or ()))
            names.update(view.card(c).name for c in (p.known_hand or ()))
        return {k: v for k, v in (getattr(view, "equip_text", None) or {}).items() if k in names}

    def player(self, view, p) -> List[str]:
        lines = super().player(view, p)
        if p.seat == view.viewer:
            for s, cids in (p.known_by or {}).items():
                who = "对手" if view.is_enemy(s) else f"{s} 号位"
                lines.append(f"  {who}知道你手里有：" + "、".join(
                    card_str(view, c) + (f"（{known_note(view, c, p.seat)}）" if known_note(view, c, p.seat) else "")
                    for c in cids))
        elif p.known_hand is not None:
            if p.known_hand:
                known = "、".join(card_str(view, c) + (f"（{known_note(view, c, p.seat)}）" if known_note(view, c, p.seat) else "")
                                 for c in p.known_hand)
                lines = [(f"  手牌 {p.hand_count} 张，其中你知道的：{known}" if l.startswith("  手牌数：") else l)
                         for l in lines]
            else:
                lines = [(f"  手牌 {p.hand_count} 张（都不知道是什么）" if l.startswith("  手牌数：") else l) for l in lines]
        return lines

    def history(self, view) -> str:
        if not self.recent_turns:
            return super().history(view)
        ents = view.log_entries
        if not ents:
            return ""
        first = max(1, view.turn - self.recent_turns + 1)
        whose: Dict[int, Optional[int]] = {}
        for e in ents:
            if e["kind"] == "turn" and e.get("seat") is not None:
                whose[e["turn"]] = e["seat"]
        keep = [e for e in ents if e["turn"] >= first and e["text"]
                and (self.keep_flow or e["kind"] not in FLOW_KINDS)]
        cut = len(keep) > self.max_events
        keep = keep[-self.max_events:]
        if not keep:
            return "【最近发生的事】\n（本回合和上一回合没有出牌、响应、伤害之类的事）"
        out: List[str] = []
        cur = None
        for e in keep:
            if e["turn"] != cur:
                cur = e["turn"]
                s = whose.get(cur)
                if s is None:
                    out.append(f"第 {cur} 回合：")
                else:
                    gen = view.players[s].general
                    out.append(f"第 {cur} 回合（{'你' if s == view.viewer else '对手'} {gen} 的回合）：")
            out.append("- " + e["text"])
        head = f"【最近发生的事】（第 {first} 回合起" + ("，太多了只列最近的" if cut else "") + "；不含进入/结束阶段这类流程记录）" \
            if not self.keep_flow else f"【最近发生的事】（第 {first} 回合起）"
        return head + "\n" + "\n".join(out)
