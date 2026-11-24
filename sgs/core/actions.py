"""通用结算帧：回合、出牌、使用牌、无懈可击、响应、伤害、濒死、判定、弃牌等。

这些帧只依赖注册表里的规则对象，不认识任何具体武将；卡牌名只出现
“杀/闪/桃/无懈可击”这几个基础规则本身就要用到的名字。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import registry as R
from .base import Decision, Ev, Frame, Option, yes_no
from .cards import VCard
from .game import SLOT_NAMES

SLASH, DODGE, PEACH, NULLIFY = "杀", "闪", "桃", "无懈可击"
PASS = ("pass",)


# ---------------------------------------------------------------------- 工具函数
def trigger(g, event: str, ev, only: Optional[int] = None) -> bool:
    """如有技能监听 event，就压入一个 TriggerFrame。返回是否压栈。"""
    for s in g.alive_seats():
        if only is not None and s != only:
            continue
        for name in g.skills_of(s):
            if event in R.SKILLS[name].events:
                g.push(TriggerFrame(event, ev, only))
                return True
    return False


def card_options(g, seat: int, names: Sequence[str], ctx: Optional[dict] = None) -> List[VCard]:
    """seat 能拿出来当作 names 中某种牌的所有方式（手牌 + 转化技）。"""
    p = g.players[seat]
    names = tuple(names)
    out = [VCard.real(g.cards[c]) for c in p.hand if g.cards[c].name in names]
    ctx = ctx or {}
    for sk in g.skills_of(seat):
        out.extend(R.SKILLS[sk].view_as(g, seat, names, ctx))
    seen, uniq = set(), []
    for vc in out:
        k = vc.key()
        if k not in seen:
            seen.add(k)
            uniq.append(vc)
    return uniq


def vc_option(g, vc: VCard, targets: Tuple[int, ...] = (), prefix: str = "", kind: str = "card") -> Option:
    label = prefix + vc.describe(g)
    if targets:
        label += " → " + "、".join(g.pname(t) for t in targets)
    return Option((kind, vc.name, vc.subcards, vc.skill, tuple(targets)), label, vc)


def respond(g, seat: int, vc: VCard, used: bool = True) -> None:
    """把响应用的牌放入处理区并记录（之后由调用者清理处理区）。"""
    g.to_processing(vc.subcards)
    if vc.name == SLASH:
        g.players[seat].turn["slash_any"] = True
    if g.logging:
        g.log(f"{g.pname(seat)} {'使用' if used else '打出'}了 {vc.describe(g)}", "respond",
              seat=seat, card=vc.name)


def play_options(g, seat: int) -> List[Option]:
    """出牌阶段的全部合法选项。"""
    p = g.players[seat]
    cands = [VCard.real(g.cards[c]) for c in p.hand]
    for sk in g.skills_of(seat):
        cands.extend(R.SKILLS[sk].view_as(g, seat, None, {"mode": "play"}))
    opts: List[Option] = []
    seen = set()
    for vc in cands:
        if vc.key() in seen:
            continue
        seen.add(vc.key())
        spec = R.CARDS.get(vc.name)
        if spec is None or spec.response_only:
            continue
        for tg in spec.play_targets(g, seat, vc):
            opts.append(vc_option(g, vc, tg, "使用 ", kind="use"))
    for sk in g.skills_of(seat):
        s = R.SKILLS[sk]
        if s.can_activate(g, seat):
            opts.append(Option(("skill", sk), f"发动【{sk}】"))
    opts.append(Option(("end",), "结束出牌阶段"))
    return opts


def can_slash(g, user: int, target: int, vc: VCard, check_range: bool = True) -> bool:
    if target == user or not g.players[target].alive:
        return False
    if g.prohibited(user, target, vc):
        return False
    if check_range and not g.in_attack_range(user, target):
        return False
    return True


# ---------------------------------------------------------------------- 触发
class CallFrame(Frame):
    """调用技能的 on_trigger（单步技能）。"""

    def __init__(self, skill: str, owner: int, event: str, ev):
        self.skill, self.owner, self.event, self.ev = skill, owner, event, ev

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            R.SKILLS[self.skill].on_trigger(g, self.owner, self.event, self.ev)
        else:
            g.done()


class TriggerFrame(Frame):
    """按座次（从当前回合角色开始）依次询问/发动监听 event 的技能。"""

    def __init__(self, event: str, ev, only: Optional[int] = None):
        self.event, self.ev, self.only = event, ev, only

    def step(self, g):
        if self.stage == 0:
            self.queue = [(s, n) for s in g.alive_seats() if self.only is None or s == self.only
                          for n in g.skills_of(s) if self.event in R.SKILLS[n].events]
            self.i = 0
            self.stage = 1
        if self.stage == 1:
            while self.i < len(self.queue):
                s, name = self.queue[self.i]
                self.i += 1
                sk = R.SKILLS[name]
                if not g.players[s].alive or name not in g.skills_of(s):
                    continue
                if not sk.can_trigger(g, s, self.event, self.ev):
                    continue
                if sk.is_compulsory(self.event):
                    if sk.compulsory:
                        g.log(f"{g.pname(s)} 的【{name}】被触发", "skill", seat=s, skill=name)
                    f = sk.make_frame(g, s, self.event, self.ev)
                    if f is not None:
                        g.push(f)
                    return
                self.cur = (s, name)
                self.stage = 2
                g.ask(yes_no(s, sk.trigger_prompt(g, s, self.event, self.ev),
                             {"skill": name, "event": self.event}))
                return
            g.done()
            return
        # stage 2: 是否发动
        ans = g.answer()
        self.stage = 1
        s, name = self.cur
        if ans.key != ("yes",):
            g.log(f"{g.pname(s)} 没有发动【{name}】", "pass", seat=s, skill=name)
        if ans.key == ("yes",):
            g.log(f"{g.pname(s)} 发动了【{name}】", "skill", seat=s, skill=name)
            f = R.SKILLS[name].make_frame(g, s, self.event, self.ev)
            if f is not None:
                g.push(f)


class QueuedTrigger(Frame):
    """失去牌后排队触发的事件（连营等），结束时恢复父帧看到的 g.ret。"""

    def __init__(self, event: str, seat: int, saved_ret):
        self.event, self.seat, self.saved = event, seat, saved_ret

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            zones = g.players[self.seat].marks.pop("_lostq", [])
            trigger(g, self.event, Ev(seat=self.seat, zones=zones), only=self.seat)
        else:
            g.done(self.saved)


# ---------------------------------------------------------------------- 选择
class ChoiceFrame(Frame):
    """一般单选，返回选中项的 key。"""

    def __init__(self, seat: int, choices: Sequence[Tuple[tuple, str]], prompt: str, ctx=None):
        self.seat, self.choices, self.prompt, self.ctx = seat, list(choices), prompt, ctx or {}

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.ask(Decision(self.seat, "choose", self.prompt, [Option(k, l) for k, l in self.choices], self.ctx))
        else:
            g.done(g.answer().key)


class SelectCardsFrame(Frame):
    """从 candidates 里逐张选 min..max 张（不移动），返回 id 列表。"""

    def __init__(self, seat: int, candidates: Sequence[int], min_n: int, max_n: int, prompt: str, ctx=None):
        self.seat, self.cands, self.min_n, self.max_n = seat, list(candidates), min_n, max_n
        self.prompt, self.ctx = prompt, ctx or {}
        self.chosen: List[int] = []

    def step(self, g):
        if self.stage == 1:
            ans = g.answer()
            if ans.key[0] == "stop":
                g.done(self.chosen)
                return
            self.chosen.append(ans.key[1])
        self.stage = 1
        rest = [c for c in self.cands if c not in self.chosen]
        if len(self.chosen) >= self.max_n or not rest:
            g.done(self.chosen)
            return
        opts = [Option(("card", c), g.cstr(c)) for c in rest]
        if len(self.chosen) >= self.min_n:
            opts.append(Option(("stop",), "确定" if self.chosen else "不选"))
        prompt = f"{self.prompt}（已选 {len(self.chosen)}）" if self.chosen else self.prompt
        g.ask(Decision(self.seat, "choose", prompt, opts, dict(self.ctx, chosen=list(self.chosen))))


class DiscardFrame(Frame):
    """令 seat 弃置 n 张牌（zones 默认手牌）。"""

    def __init__(self, seat: int, n: int, zones: str = "h", reason: str = "", prompt: str = ""):
        self.seat, self.n, self.zones, self.reason = seat, n, zones, reason
        self.prompt = prompt or f"请弃置 {n} 张牌"

    def step(self, g):
        p = g.players[self.seat]
        if self.stage == 0:
            cands = list(p.hand) if "h" in self.zones else []
            if "e" in self.zones:
                cands += list(p.equips.values())
            n = min(self.n, len(cands))
            self.stage = 1
            g.push(SelectCardsFrame(self.seat, cands, n, n, self.prompt, {"reason": self.reason or "discard"}))
            return
        chosen = g.ret
        if chosen:
            g.to_discard(chosen)
            g.log(f"{g.pname(self.seat)} 弃置了 " + "、".join(g.cstr(c) for c in chosen), "discard",
                  seat=self.seat, n=len(chosen))
        g.done(chosen)


class ChooseCardFrame(Frame):
    """chooser 选择 target 区域里的一张牌（手牌背面朝上，随机取一张）。返回 id 或 None。"""

    def __init__(self, chooser: int, target: int, zones: str = "hej", prompt: str = "", reason: str = ""):
        self.chooser, self.target, self.zones = chooser, target, zones
        self.prompt, self.reason = prompt or "选择一张牌", reason

    def step(self, g):
        p = g.players[self.target]
        if self.stage == 0:
            opts = []
            if "h" in self.zones and p.hand:
                own = self.chooser == self.target
                if own:
                    opts += [Option(("card", c), "手牌 " + g.cstr(c)) for c in p.hand]
                else:
                    opts.append(Option(("hand",), f"一张手牌（共 {len(p.hand)} 张，随机）"))
            if "e" in self.zones:
                opts += [Option(("card", c), f"{SLOT_NAMES[s]} {g.cstr(c)}") for s, c in p.equips.items()]
            if "j" in self.zones:
                opts += [Option(("card", c), f"判定区 {g.cstr(c)}") for c, _ in p.judges]
            if not opts:
                g.done(None)
                return
            self.stage = 1
            g.ask(Decision(self.chooser, "choose", self.prompt, opts,
                           {"target": self.target, "reason": self.reason}))
            return
        ans = g.answer()
        if ans.key == ("hand",):
            g.done(g.rng.choice(p.hand))
        else:
            g.done(ans.key[1])


# ---------------------------------------------------------------------- 回合
class TurnFrame(Frame):
    def __init__(self, seat: int):
        self.seat = seat

    def step(self, g):
        p = g.players[self.seat]
        st = self.stage
        if st == 0:
            if g.turn_count >= g.max_turns:
                g.finish(None, f"达到 {g.max_turns} 回合上限，平局")
            g.current = self.seat
            g.turn_count += 1
            for q in g.players:
                q.turn = {}
            g.phase = "start"
            g.log(f"—— 第 {g.turn_count} 回合：{g.pname(self.seat)} ——", "turn", seat=self.seat)
            self.ev = Ev(seat=self.seat, skip=p.turn.setdefault("skip", set()))
            self.stage = 1
            trigger(g, "turn_start", self.ev)
            return
        if not p.alive:
            return self._end(g)
        if st == 1:
            g.phase = "prepare"
            self.stage = 2
            trigger(g, "phase_prepare", self.ev)
        elif st == 2:
            g.phase = "judge"
            self.stage = 3
            if "judge" in self.ev.skip:
                g.log(f"{g.pname(self.seat)} 跳过判定阶段", "skip", seat=self.seat)
            elif p.judges:
                g.log(f"{g.pname(self.seat)} 进入判定阶段", "phase", seat=self.seat, phase="judge")
                g.push(JudgePhaseFrame(self.seat, self.ev))
        elif st == 3:
            g.phase = "draw"
            self.stage = 5
            if "draw" in self.ev.skip:
                g.log(f"{g.pname(self.seat)} 跳过摸牌阶段", "skip", seat=self.seat)
                return
            self.draw = Ev(seat=self.seat, count=g.mode.draw_phase_count(g, self.seat, 2))
            self.stage = 4
            trigger(g, "draw_phase", self.draw)
        elif st == 4:
            if self.draw.count > 0:
                g.draw(self.seat, self.draw.count, "摸牌阶段")
            else:
                g.log(f"{g.pname(self.seat)} 摸牌阶段不摸牌", "draw", seat=self.seat)
            self.stage = 5
        elif st == 5:
            g.phase = "play"
            self.stage = 6
            if "play" not in self.ev.skip:
                g.log(f"{g.pname(self.seat)} 进入出牌阶段（手牌 {len(p.hand)}）", "phase", seat=self.seat, phase="play")
                g.push(PlayPhaseFrame(self.seat))
            else:
                g.log(f"{g.pname(self.seat)} 跳过出牌阶段", "skip", seat=self.seat)
        elif st == 6:
            g.phase = "discard"
            self.stage = 7
            trigger(g, "phase_discard", self.ev)
        elif st == 7:
            self.stage = 8
            if "discard" in self.ev.skip:
                g.log(f"{g.pname(self.seat)} 跳过弃牌阶段", "skip", seat=self.seat)
                return
            n = len(p.hand) - g.max_hand(self.seat)
            g.log(f"{g.pname(self.seat)} 弃牌阶段：手牌 {len(p.hand)}，上限 {g.max_hand(self.seat)}"
                  + (f"，需弃 {n} 张" if n > 0 else "，不用弃牌"), "phase", seat=self.seat, phase="discard")
            if n > 0:
                g.push(DiscardFrame(self.seat, n, "h", "discard_phase", f"弃牌阶段：请弃置 {n} 张手牌"))
        elif st == 8:
            g.phase = "finish"
            self.stage = 9
            trigger(g, "phase_finish", self.ev)
        else:
            self._end(g)

    def _end(self, g):
        p = g.players[self.seat]
        if p.alive:
            g.log(f"{g.pname(self.seat)} 回合结束（手牌 {len(p.hand)}，体力 {p.hp}/{p.max_hp}）", "phase",
                  seat=self.seat, phase="end")
        g.phase = ""
        nxt = g.next_alive(self.seat)
        g.done()
        g.push(TurnFrame(nxt))


class JudgePhaseFrame(Frame):
    """判定阶段：从最后放入的延时锦囊开始依次结算。"""

    def __init__(self, seat: int, turn_ev):
        self.seat, self.turn_ev = seat, turn_ev

    def step(self, g):
        p = g.players[self.seat]
        if self.stage == 0:
            self.todo = list(reversed(p.judges))  # 后放入的先结算；只结算阶段开始时已有的
            self.stage = 1
        while self.todo:
            cid, as_name = self.todo.pop(0)
            if not p.alive or (cid, as_name) not in p.judges:
                continue
            g.to_processing([cid])
            f = R.CARDS[as_name].delayed_effect(g, self.seat, cid, as_name)
            if f is not None:
                g.push(f)
                return
        g.done()


class PlayPhaseFrame(Frame):
    def __init__(self, seat: int):
        self.seat = seat

    def step(self, g):
        if self.stage == 1:
            ans = g.answer()
            self.stage = 0
            k = ans.key
            if k[0] == "end":
                g.log(f"{g.pname(self.seat)} 结束出牌阶段", "phase", seat=self.seat, phase="play_end")
                g.done()
                return
            if k[0] == "use":
                g.push(UseCardFrame(self.seat, ans.data, k[4]))
                return
            if k[0] == "skill":
                g.players[self.seat].turn.setdefault("skills_used", []).append(k[1])
                f = R.SKILLS[k[1]].activate(g, self.seat)
                if f is not None:
                    g.push(f)
                return
        if not g.players[self.seat].alive:
            g.done()
            return
        self.stage = 1
        g.ask(Decision(self.seat, "play", "出牌阶段：选择要使用的牌或技能", play_options(g, self.seat)))


# ---------------------------------------------------------------------- 使用牌
class UseCardFrame(Frame):
    """使用一张牌（含转化牌）。use 事件字段：
    user, card(VCard), targets, info{target: Ev(shan, no_dodge, cancelled, extra, ignore_armor)}, extra(五谷亮出的牌等)
    """

    def __init__(self, user: int, vc: VCard, targets: Sequence[int], ctx: Optional[dict] = None):
        self.user, self.vc, self.ctx = user, vc, dict(ctx or {})
        tg, extra = R.CARDS[vc.name].split_targets(tuple(targets))
        self.targets = tuple(tg)
        self.ctx.update(extra)

    def step(self, g):
        st = self.stage
        vc = self.vc
        spec = R.CARDS[vc.name]
        if st == 0:
            p = g.players[self.user]
            self.use = use = Ev(user=self.user, card=vc, targets=list(self.targets), extra=[],
                                info={t: Ev(shan=1, no_dodge=False, cancelled=False, extra=0, ignore_armor=False, duel_need={})
                                      for t in self.targets}, **self.ctx)
            if g.logging:
                if spec.type == "equip":
                    text = f"{g.pname(self.user)} 装备了 {vc.describe(g)}"
                else:
                    tg = ("，目标 " + "、".join(g.pname(t) for t in self.targets)) if self.targets else ""
                    text = f"{g.pname(self.user)} 使用了 {vc.describe(g)}{tg}"
                g.log(text, "use", seat=self.user, card=vc.name, targets=list(self.targets))
            if vc.name == SLASH:
                if not self.ctx.get("extra_slash"):
                    p.turn["slash"] = p.turn.get("slash", 0) + 1
                p.turn["slash_any"] = True
            if spec.type == "equip":
                cid = vc.subcards[0]
                g.remove_card(cid)
                old = p.equips.get(spec.subtype)
                if old is not None:
                    g.to_discard([old])
                p.equips[spec.subtype] = cid
                self.stage = 9
            elif spec.type == "delayed":
                cid = vc.subcards[0]
                g.remove_card(cid)
                g.players[self.targets[0]].judges.append((cid, vc.name))
                self.stage = 9
            else:
                g.to_processing(vc.subcards)
                self.stage = 1
            trigger(g, "card_used", use)
            return
        use = self.use
        if st == 1:
            self.stage = 2
            for t in reversed(self.targets):
                trigger(g, "target_confirm", Ev(use=use, target=t, info=use.info[t]))
        elif st == 2:
            self.stage = 3
            self.i = 0
            f = spec.before_effects(g, use)
            if f is not None:
                g.push(f)
        elif st == 3:
            while self.i < len(self.targets):
                t = self.targets[self.i]
                if use.info[t].cancelled or not g.players[t].alive:
                    self.i += 1
                    continue
                if spec.type == "trick" and not getattr(use, "no_nullify", False):
                    self.stage = 4
                    g.push(NullifyFrame(vc.name, self.user, t))
                    return
                self.stage = 5
                return
            self.stage = 6
        elif st == 4:
            if g.ret:
                self.i += 1
                self.stage = 3
            else:
                self.stage = 5
        elif st == 5:
            t = self.targets[self.i]
            self.i += 1
            self.stage = 3
            f = spec.effect(g, use, t)
            if f is not None:
                g.push(f)
        elif st == 6:
            spec.after_effects(g, use)
            g.clear_processing(list(vc.subcards) + list(use.extra))
            self.stage = 9
            trigger(g, "use_finished", use)
        else:
            g.done(self.use)


class NullifyFrame(Frame):
    """无懈可击询问链。返回最终是否被抵消。"""

    def __init__(self, trick: str, user: Optional[int], target: int):
        self.trick, self.user, self.target = trick, user, target
        self.count = 0

    def step(self, g):
        if self.stage == 0:
            self.order = g.alive_seats()
            self.idx = 0
            self.stage = 1
        if self.stage == 1:
            while self.idx < len(self.order):
                s = self.order[self.idx]
                if not g.players[s].alive:
                    self.idx += 1
                    continue
                vcs = card_options(g, s, (NULLIFY,), {"mode": "use"})
                if not vcs:
                    self.idx += 1
                    continue
                state = "已被抵消" if self.count % 2 else "将要生效"
                desc = f"【{self.trick}】对 {g.pname(self.target)}"
                if self.user is not None:
                    desc = f"{g.pname(self.user)} 的" + desc
                opts = [vc_option(g, vc, (), "使用 ") for vc in vcs] + [Option(PASS, "不使用")]
                self.cur = s
                self.stage = 2
                g.ask(Decision(s, "respond", f"{desc}{state}，是否使用无懈可击？", opts,
                               {"names": [NULLIFY], "trick": self.trick, "user": self.user,
                                "target": self.target, "nullified": bool(self.count % 2)}))
                return
            if self.count:
                res = "被抵消" if self.count % 2 else "仍然生效"
                g.log(f"无懈可击结算完毕：【{self.trick}】对 {g.pname(self.target)} {res}", "info",
                      trick=self.trick, target=self.target, nullified=bool(self.count % 2))
            g.done(bool(self.count % 2))
            return
        if self.stage == 2:
            ans = g.answer()
            if ans.key == PASS:
                g.log("", "pass", private={self.cur: f"{g.pname(self.cur)} 没有使用无懈可击"}, seat=self.cur)
                self.idx += 1
                self.stage = 1
                return
            self.vc = ans.data
            respond(g, self.cur, self.vc, True)
            self.count += 1
            self.idx = 0
            self.stage = 3
            trigger(g, "card_used", Ev(user=self.cur, card=self.vc, targets=[], response=True, extra=[], info={}))
            return
        # stage 3
        g.clear_processing(self.vc.subcards)
        self.stage = 1


class AskForCardFrame(Frame):
    """要求 seat 使用/打出一张 names 中的牌。返回 VCard 或 None。
    ctx 会传给转化技和响应技能（例如 ignore_armor、dying、source）。"""

    def __init__(self, seat: int, names: Sequence[str], ctx: Optional[dict] = None, used: bool = True,
                 prompt: str = ""):
        self.seat, self.names, self.used = seat, tuple(names), used
        self.ctx = dict(ctx or {})
        self.ctx.setdefault("mode", "use" if used else "respond")
        self.prompt = prompt or f"请{'使用' if used else '打出'}一张{'/'.join(self.names)}"
        self.skipped: List[str] = []

    def step(self, g):
        st = self.stage
        if st == 0:
            if not g.players[self.seat].alive:
                g.done(None)
                return
            vcs = card_options(g, self.seat, self.names, self.ctx)
            opts = [vc_option(g, vc, (), "使用 " if self.used else "打出 ") for vc in vcs]
            for sk in g.skills_of(self.seat):
                if sk in self.skipped:
                    continue
                for label in R.SKILLS[sk].response_options(g, self.seat, self.names, self.ctx):
                    opts.append(Option(("skill", sk), label))
            opts.append(Option(PASS, "不出"))
            self.n_opts = len(opts)
            self.stage = 1
            g.ask(Decision(self.seat, "respond", self.prompt, opts, dict(self.ctx, names=list(self.names))))
        elif st == 1:
            ans = g.answer()
            if ans.key == PASS:
                verb = "使用" if self.used else "打出"
                what = "/".join(self.names)
                why = "没有可用的牌" if self.n_opts == 1 else "选择不出"
                g.log(f"{g.pname(self.seat)} 没有{verb}{what}", "pass",
                      private={self.seat: why}, seat=self.seat, names=list(self.names))
                g.done(None)
                return
            if ans.key[0] == "skill":
                self.skipped.append(ans.key[1])
                self.stage = 3
                f = R.SKILLS[ans.key[1]].response_frame(g, self.seat, self.names, self.ctx)
                g.push(f)
                return
            self.vc = ans.data
            respond(g, self.seat, self.vc, self.used)
            self.stage = 2
            trigger(g, "card_used" if self.used else "card_played",
                    Ev(user=self.seat, card=self.vc, targets=[], response=True, extra=[], info={}, ctx=self.ctx))
        elif st == 2:
            g.clear_processing(self.vc.subcards)
            g.done(self.vc)
        else:
            vc = g.ret
            if vc is not None:
                g.done(vc)
            else:
                self.stage = 0


# ---------------------------------------------------------------------- 伤害、体力
class DamageFrame(Frame):
    """伤害结算。事件：damage_cause（来源）→ damage_inflict（目标）→ 扣体力/濒死 → damaged（目标）→ damage_done（来源）。"""

    def __init__(self, source: Optional[int], target: int, amount: int = 1, card: Optional[VCard] = None,
                 nature: str = "normal", reason: str = ""):
        self.ev = Ev(source=source, target=target, amount=amount, card=card, nature=nature,
                     reason=reason, prevented=False)

    def step(self, g):
        ev = self.ev
        st = self.stage
        if st == 0:
            self.stage = 1
            if ev.source is not None and g.players[ev.source].alive:
                trigger(g, "damage_cause", ev)
        elif st == 1:
            if ev.prevented or ev.amount <= 0 or not g.players[ev.target].alive:
                g.done(0)
                return
            self.stage = 2
            trigger(g, "damage_inflict", ev)
        elif st == 2:
            if ev.prevented or ev.amount <= 0:
                g.done(0)
                return
            t = g.players[ev.target]
            t.hp -= ev.amount
            src = f"{g.pname(ev.source)} 对 " if ev.source is not None else ""
            nat = {"thunder": "雷电", "fire": "火焰"}.get(ev.nature, "")
            g.log(f"{src}{g.pname(ev.target)} 造成 {ev.amount} 点{nat}伤害（{t.hp}/{t.max_hp}）", "damage",
                  source=ev.source, target=ev.target, n=ev.amount)
            self.stage = 3
            if t.hp <= 0:
                g.push(DyingFrame(ev.target, ev.source))
        elif st == 3:
            self.stage = 4
            if ev.source is not None:
                trigger(g, "damage_done", ev)
            trigger(g, "damaged", ev)
        else:
            g.done(ev.amount)


class LoseHpFrame(Frame):
    def __init__(self, seat: int, n: int = 1):
        self.seat, self.n = seat, n

    def step(self, g):
        if self.stage == 0:
            p = g.players[self.seat]
            p.hp -= self.n
            g.log(f"{g.pname(self.seat)} 失去 {self.n} 点体力（{p.hp}/{p.max_hp}）", "losehp", seat=self.seat)
            self.stage = 1
            if p.hp <= 0:
                g.push(DyingFrame(self.seat, None))
        else:
            g.done()


class DyingFrame(Frame):
    """濒死：从当前回合角色开始依次询问是否使用桃，直到体力 > 0 或都不救。"""

    def __init__(self, seat: int, killer: Optional[int]):
        self.seat, self.killer = seat, killer

    def step(self, g):
        p = g.players[self.seat]
        st = self.stage
        if st == 0:
            g.log(f"{g.pname(self.seat)} 进入濒死状态", "dying", seat=self.seat)
            self.order = g.alive_seats()
            self.idx = 0
            self.stage = 1
            trigger(g, "dying", Ev(seat=self.seat, killer=self.killer))
            return
        if st == 1:
            if p.hp > 0:
                g.done()
                return
            if self.idx >= len(self.order):
                self.stage = 3
                g.push(DeathFrame(self.seat, self.killer))
                return
            asker = self.order[self.idx]
            if not g.players[asker].alive:
                self.idx += 1
                return
            self.stage = 2
            g.push(AskForCardFrame(asker, (PEACH,), {"dying": self.seat, "mode": "use"}, True,
                                   f"{g.pname(self.seat)} 濒死（体力 {p.hp}），是否使用桃？"))
        elif st == 2:
            if g.ret is not None:
                g.recover(self.seat, 1, self.order[self.idx])
            else:
                self.idx += 1
            self.stage = 1
        else:
            g.done()


class DeathFrame(Frame):
    def __init__(self, seat: int, killer: Optional[int]):
        self.seat, self.killer = seat, killer

    def step(self, g):
        p = g.players[self.seat]
        if self.stage == 0:
            p.alive = False
            g.log(f"{g.pname(self.seat)} 阵亡", "death", seat=self.seat)
            self.stage = 1
            trigger(g, "death", Ev(seat=self.seat, killer=self.killer))
            return
        cards = list(p.hand) + list(p.equips.values()) + [c for c, _ in p.judges]
        p.hand, p.equips, p.judges = [], {}, []
        g.discard.extend(cards)
        g.done()
        g.mode.on_death(g, self.seat, self.killer)


# ---------------------------------------------------------------------- 判定
class JudgeFrame(Frame):
    """进行一次判定，返回最终判定牌 id。事件：judge_retrial（改判）→ judge_done（生效后，如天妒）。"""

    def __init__(self, seat: int, reason: str):
        self.seat, self.reason = seat, reason

    def step(self, g):
        if self.stage == 0:
            cid = g.draw_top()
            g.processing.append(cid)
            self.ev = Ev(seat=self.seat, reason=self.reason, card=cid)
            g.log(f"{g.pname(self.seat)} 的【{self.reason}】判定牌为 {g.cstr(cid)}", "judge",
                  seat=self.seat, reason=self.reason, card=cid)
            self.stage = 1
            trigger(g, "judge_retrial", self.ev)
        elif self.stage == 1:
            self.stage = 2
            trigger(g, "judge_done", self.ev)
        else:
            if self.ev.card in g.processing:
                g.clear_processing([self.ev.card])
            g.done(self.ev.card)


JUDGE_GOOD: Dict[str, object] = {}  # 判定原因 -> 函数(CardInfo) -> 对判定者是否有利（给 AI 改判参考）


def judge_good(reason: str):
    def deco(fn):
        JUDGE_GOOD[reason] = fn
        return fn
    return deco
