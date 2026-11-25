"""标准包 25 名武将的技能（主公技在 1v1 中无效，未实现）。

每个技能是一个注册到 SKILLS 的类，引擎通过事件名和钩子调用，不需要改核心代码。
"""
from ...core.actions import (ChoiceFrame, DamageFrame, DiscardFrame, JudgeFrame, LoseHpFrame, SelectCardsFrame,
                             TriggerFrame, judge_good)
from ...core.base import Decision, Ev, Frame, Option, yes_no
from ...core.cards import DIAMOND, HEART, SUITS, VCard
from ...core.plugin import INF, Skill
from ...core.registry import CARDS, register_skill
from .basic import SlashEffect
from .tricks import TakeCardFrame


def _own_turn(g, owner):
    return g.current == owner


def _used(g, owner, name):
    return name in g.players[owner].turn.get("skills_used", [])


def _view(g, owner, cids, name, skill):
    return [VCard.convert(g, name, (c,), skill) for c in cids]


def _he(g, owner):
    p = g.players[owner]
    return list(p.hand) + list(p.equips.values())


def _wants(names, name):
    return names is None or name in names


# ====================================================================== 魏
@register_skill
class Jianxiong(Skill):
    name = "奸雄"
    events = ("damaged",)
    description = "你可以立即获得对你造成伤害的牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.target == owner and ev.card is not None and any(c in g.processing for c in ev.card.subcards)

    def on_trigger(self, g, owner, event, ev):
        cids = [c for c in ev.card.subcards if c in g.processing]
        g.to_hand(owner, cids, "奸雄")
        g.log(f"{g.pname(owner)} 获得了 " + "、".join(g.cstr(c) for c in cids), "obtain")


@register_skill
class Fankui(Skill):
    name = "反馈"
    events = ("damaged",)
    description = "你可以立即从对你造成伤害的来源处获得一张牌。"
    def can_trigger(self, g, owner, event, ev):
        s = ev.source
        return (ev.target == owner and s is not None and s != owner and g.players[s].alive
                and g.players[s].card_count("he") > 0)

    def make_frame(self, g, owner, event, ev):
        return TakeCardFrame(owner, ev.source, False, "he", "反馈")


@register_skill
class Guicai(Skill):
    name = "鬼才"
    events = ("judge_retrial",)
    description = "在任意角色的判定牌生效前，你可以打出一张手牌代替之。"
    def can_trigger(self, g, owner, event, ev):
        return bool(g.players[owner].hand)

    def trigger_prompt(self, g, owner, event, ev):
        return f"{g.pname(ev.seat)} 的【{ev.reason}】判定牌为 {g.cstr(ev.card)}，是否发动【鬼才】改判？"

    def trigger_ctx(self, g, owner, event, ev):
        return {"judge_seat": ev.seat, "judge_reason": ev.reason, "judge_card": ev.card}

    def make_frame(self, g, owner, event, ev):
        return GuicaiFrame(owner, ev)


class GuicaiFrame(Frame):
    def __init__(self, owner, ev):
        self.owner, self.ev = owner, ev

    def step(self, g):
        if self.stage == 0:
            p = g.players[self.owner]
            opts = [Option(("card", c), f"打出 {g.cstr(c)} 代替") for c in p.hand]
            self.stage = 1
            g.ask(Decision(self.owner, "choose",
                           f"鬼才：{g.pname(self.ev.seat)} 的【{self.ev.reason}】判定牌为 {g.cstr(self.ev.card)}，选择打出哪张手牌代替",
                           opts, {"skill": "鬼才", "judge_seat": self.ev.seat, "judge_reason": self.ev.reason,
                                  "judge_card": self.ev.card}))
            return
        ans = g.answer()
        if ans.key[0] == "card":
            new = ans.key[1]
            old = self.ev.card
            g.to_discard([old])
            g.to_processing([new])
            self.ev.card = new
            g.log(f"{g.pname(self.owner)} 发动【鬼才】，判定牌改为 {g.cstr(new)}", "skill")
        g.done()


@register_skill
class Ganglie(Skill):
    name = "刚烈"
    events = ("damaged",)
    description = "你每受到一次伤害，可以进行一次判定：若结果不为红桃，则伤害来源必须二选一：弃两张手牌，或受到你对其造成的 1 点伤害。"
    def can_trigger(self, g, owner, event, ev):
        return ev.target == owner and ev.source is not None and ev.source != owner and g.players[ev.source].alive

    def make_frame(self, g, owner, event, ev):
        return GanglieFrame(owner, ev.source)


@judge_good("刚烈")
def _ganglie_good(card):
    return card.suit != HEART


class GanglieFrame(Frame):
    def __init__(self, owner, src):
        self.owner, self.src = owner, src

    def step(self, g):
        st = self.stage
        if st == 0:
            self.stage = 1
            g.push(JudgeFrame(self.owner, "刚烈"))
        elif st == 1:
            if g.cards[g.ret].suit == HEART or not g.players[self.src].alive:
                g.done()
                return
            if len(g.players[self.src].hand) >= 2:
                self.stage = 2
                g.push(ChoiceFrame(self.src, [(("discard",), "弃置两张手牌"),
                                              (("damage",), f"受到 {g.pname(self.owner)} 的 1 点伤害")],
                                   "刚烈：请选择", {"reason": "刚烈"}))
            else:
                self.stage = 3
                g.push(DamageFrame(self.owner, self.src, 1, None, "normal", "刚烈"))
        elif st == 2:
            self.stage = 3
            if g.ret == ("discard",):
                g.push(DiscardFrame(self.src, 2, "h", "刚烈", "刚烈：弃置两张手牌"))
            else:
                g.push(DamageFrame(self.owner, self.src, 1, None, "normal", "刚烈"))
        else:
            g.done()


@register_skill
class Tuxi(Skill):
    name = "突袭"
    events = ("draw_phase",)
    description = "摸牌阶段，你可以放弃摸牌，然后从至多两名（至少一名）角色的手牌里各抽取一张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner and ev.count > 0 and any(g.players[t].hand for t in g.others(owner))

    def make_frame(self, g, owner, event, ev):
        ev.count = 0
        return TuxiFrame(owner)


class TuxiFrame(Frame):
    def __init__(self, owner):
        self.owner, self.taken = owner, []

    def step(self, g):
        if self.stage == 1:
            k = g.answer().key
            if k == ("stop",):
                g.done()
                return
            t = k[1]
            cid = g.rng.choice(g.players[t].hand)
            g.to_hand(self.owner, [cid], "突袭")
            self.taken.append(t)
            g.log(f"{g.pname(self.owner)} 获得了 {g.pname(t)} 的一张手牌", "obtain",
                  private={self.owner: g.cstr(cid), t: g.cstr(cid)})
        cands = [t for t in g.others(self.owner) if t not in self.taken and g.players[t].hand]
        if len(self.taken) >= 2 or not cands:
            g.done()
            return
        opts = [Option(("target", t), f"获得 {g.pname(t)} 的一张手牌") for t in cands]
        if self.taken:
            opts.append(Option(("stop",), "完成"))
        self.stage = 1
        g.ask(Decision(self.owner, "choose", "突袭：选择目标", opts, {"skill": "突袭"}))


@register_skill
class Luoyi(Skill):
    name = "裸衣"
    events = ("draw_phase",)          # 发动时机：摸牌阶段（询问）
    effects = ("damage_cause",)       # 后续效果：发动后本回合伤害 +1（不询问）
    description = "摸牌阶段，你可以少摸一张牌；若如此做，该回合的出牌阶段，你使用杀或决斗（你为伤害来源时）造成的伤害 +1。"
    def can_trigger(self, g, owner, event, ev):
        if event == "draw_phase":
            return ev.seat == owner and ev.count > 0
        return (ev.source == owner and g.players[owner].turn.get("luoyi") and ev.card is not None
                and ev.card.name in ("杀", "决斗") and ev.reason in ("杀", "决斗"))

    def on_trigger(self, g, owner, event, ev):
        if event == "draw_phase":
            ev.count -= 1
            g.players[owner].turn["luoyi"] = True
        else:
            ev.amount += 1
            g.log("裸衣：伤害 +1", "info")


@register_skill
class Tiandu(Skill):
    name = "天妒"
    events = ("judge_done",)
    description = "在你的判定牌生效后，你可以立即获得它。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner and ev.card in g.processing

    def on_trigger(self, g, owner, event, ev):
        g.to_hand(owner, [ev.card], "天妒")
        g.log(f"{g.pname(owner)} 获得了判定牌 {g.cstr(ev.card)}", "obtain")


@register_skill
class Yiji(Skill):
    name = "遗计"
    events = ("damaged",)
    description = "你每受到 1 点伤害，可以摸两张牌，将其中的一张交给任意一名角色，然后将另一张交给任意一名角色。"
    def can_trigger(self, g, owner, event, ev):
        return ev.target == owner

    def make_frame(self, g, owner, event, ev):
        return YijiFrame(owner, ev.amount)


class YijiFrame(Frame):
    """每点伤害：摸两张（只有自己看得到），再逐张决定留下或交给别人。第二点起询问是否继续。"""

    def __init__(self, owner, times):
        self.owner, self.times, self.k = owner, times, 0
        self.cards = []

    def _draw(self, g):
        self.k += 1
        self.cards = g.draw(self.owner, 2, "遗计")

    def step(self, g):
        if self.stage == 1:
            k = g.answer().key
            cid = self.cards.pop(0)
            if k[1] != self.owner and cid in g.players[self.owner].hand:
                g.to_hand(k[1], [cid], "遗计")
                g.log(f"{g.pname(self.owner)} 将一张牌交给 {g.pname(k[1])}", "give",
                      private={self.owner: g.cstr(cid), k[1]: g.cstr(cid)})
        elif self.stage == 3:
            if g.answer().key != ("yes",):
                g.done()
                return
            self._draw(g)
        self.stage = 0
        if not g.players[self.owner].alive:
            g.done()
            return
        self.cards = [c for c in self.cards if c in g.players[self.owner].hand]
        if not self.cards:
            if self.k >= self.times:
                g.done()
                return
            if self.k > 0:
                self.stage = 3
                g.ask(yes_no(self.owner, "是否继续发动【遗计】？", {"skill": "遗计"}))
                return
            self._draw(g)
        cid = self.cards[0]
        opts = [Option(("give", t), "自己留下" if t == self.owner else f"交给 {g.pname(t)}")
                for t in g.alive_seats(self.owner)]
        self.stage = 1
        g.ask(Decision(self.owner, "choose", f"遗计：分配 {g.cstr(cid)}", opts, {"skill": "遗计", "card": cid}))


@register_skill
class Qingguo(Skill):
    name = "倾国"
    description = "你可以将你的黑色手牌当闪使用（或打出）。"
    def view_as(self, g, owner, names, ctx):
        if names is None or "闪" not in names:
            return []
        return _view(g, owner, [c for c in g.players[owner].hand if g.cards[c].color == "black"], "闪", self.name)


@register_skill
class Luoshen(Skill):
    name = "洛神"
    events = ("phase_prepare",)
    description = "回合开始阶段，你可以进行判定：若为黑色，立即获得此生效后的判定牌，并可以再次使用洛神；如此反复，直到出现红色或你不愿意判定了为止。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner

    def make_frame(self, g, owner, event, ev):
        return LuoshenFrame(owner)


@judge_good("洛神")
def _luoshen_good(card):
    return card.color == "black"


class LuoshenFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(JudgeFrame(self.owner, "洛神"))
        elif self.stage == 1:
            cid = g.ret
            if g.cards[cid].color != "black":
                g.done()
                return
            if g.locate(cid)[0] in ("discard", "processing"):
                g.to_hand(self.owner, [cid], "洛神")
                g.log(f"{g.pname(self.owner)} 获得了 {g.cstr(cid)}", "obtain")
            self.stage = 2
            g.ask(yes_no(self.owner, "是否继续发动【洛神】？", {"skill": "洛神"}))
        else:
            if g.answer().key == ("yes",):
                self.stage = 0
            else:
                g.done()


# ====================================================================== 蜀
@register_skill
class Rende(Skill):
    name = "仁德"
    description = "出牌阶段，你可以将任意数量的手牌以任意分配方式交给其他角色，若你给出的牌张数不少于两张时，你回复 1 点体力。"
    def can_activate(self, g, owner):
        return bool(g.players[owner].hand) and bool(g.others(owner))

    def activate(self, g, owner):
        return RendeFrame(owner)


class RendeFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        p = g.players[self.owner]
        if self.stage == 0:
            self.stage = 1
            g.push(SelectCardsFrame(self.owner, p.hand, 1, len(p.hand), "仁德：选择要给出的手牌"))
        elif self.stage == 1:
            self.cards = g.ret
            self.stage = 2
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in g.others(self.owner)],
                               "仁德：交给谁？", {"skill": "仁德"}))
        else:
            t = g.ret[1]
            g.to_hand(t, self.cards, "仁德")
            g.log(f"{g.pname(self.owner)} 将 {len(self.cards)} 张手牌交给 {g.pname(t)}", "give",
                  private={self.owner: "、".join(g.cstr(c) for c in self.cards),
                           t: "、".join(g.cstr(c) for c in self.cards)})
            before = p.turn.get("rende", 0)
            p.turn["rende"] = before + len(self.cards)
            if before < 2 <= p.turn["rende"]:
                g.recover(self.owner, 1)
            g.done()


@register_skill
class Wusheng(Skill):
    name = "武圣"
    description = "你可以将你的任意一张红色牌当杀使用或打出。"
    def view_as(self, g, owner, names, ctx):
        if not _wants(names, "杀"):
            return []
        return _view(g, owner, [c for c in _he(g, owner) if g.cards[c].color == "red"], "杀", self.name)


@register_skill
class Paoxiao(Skill):
    name = "咆哮"
    description = "出牌阶段，你可以使用任意数量的杀。"
    def slash_limit_mod(self, g, owner):
        return INF


@register_skill
class Guanxing(Skill):
    name = "观星"
    events = ("phase_prepare",)
    description = "回合开始阶段，你可以观看牌堆顶的 X 张牌（X 为存活角色的数量且最多为 5），将其中任意数量的牌以任意顺序置于牌堆顶，其余以任意顺序置于牌堆底。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner

    def make_frame(self, g, owner, event, ev):
        return GuanxingFrame(owner)


class GuanxingFrame(Frame):
    def __init__(self, owner):
        self.owner, self.top, self.bottom = owner, [], []

    def step(self, g):
        if self.stage == 0:
            n = min(5, len(g.alive_seats()))
            self.cards = g.peek_top(n)  # 安排完之前这些牌仍留在牌堆里
            g.log(f"{g.pname(self.owner)} 观看了牌堆顶的 {len(self.cards)} 张牌", "info",
                  private={self.owner: "、".join(g.cstr(c) for c in self.cards)})
            self.stage = 1
        else:
            k = g.answer().key
            self.cards.remove(k[1])
            (self.top if k[0] == "top" else self.bottom).append(k[1])
        if not self.cards:
            for c in self.top + self.bottom:
                g.deck.remove(c)
            for c in reversed(self.top):
                g.deck.append(c)
            for c in self.bottom:
                g.deck.insert(0, c)
            g.log(f"{g.pname(self.owner)} 将 {len(self.top)} 张置于牌堆顶，{len(self.bottom)} 张置于牌堆底", "info")
            g.done()
            return
        opts = []
        for c in self.cards:
            opts.append(Option(("top", c), f"{g.cstr(c)} 置于牌堆顶（第 {len(self.top) + 1} 张）"))
            opts.append(Option(("bottom", c), f"{g.cstr(c)} 置于牌堆底"))
        g.ask(Decision(self.owner, "choose", "观星：安排牌堆", opts,
                       {"skill": "观星", "top": list(self.top), "bottom": list(self.bottom)}))


@register_skill
class Kongcheng(Skill):
    name = "空城"
    locked = True
    description = "锁定技，当你没有手牌时，你不能成为杀或决斗的目标。"
    def prohibit(self, g, owner, user, target, vc):
        return target == owner and not g.players[owner].hand and vc.name in ("杀", "决斗")


@register_skill
class Longdan(Skill):
    name = "龙胆"
    description = "你可以将你手牌的杀当闪、闪当杀使用或打出。"
    def view_as(self, g, owner, names, ctx):
        hand = g.players[owner].hand
        out = []
        if names is not None and "闪" in names:
            out += _view(g, owner, [c for c in hand if g.cards[c].name == "杀"], "闪", self.name)
        if _wants(names, "杀"):
            out += _view(g, owner, [c for c in hand if g.cards[c].name == "闪"], "杀", self.name)
        return out


@register_skill
class Mashu(Skill):
    name = "马术"
    locked = True
    description = "锁定技，当你计算与其他角色的距离时，始终 -1。"
    def distance_mod(self, g, owner, frm, to):
        return -1 if frm == owner else 0


@register_skill
class Tieqi(Skill):
    name = "铁骑"
    events = ("target_confirm",)
    description = "当你使用杀指定一名角色为目标后，你可以进行判定，若结果为红色，此杀不可被闪避。"
    def can_trigger(self, g, owner, event, ev):
        return ev.use.user == owner and ev.use.card.name == "杀"

    def make_frame(self, g, owner, event, ev):
        return TieqiFrame(owner, ev)


@judge_good("铁骑")
def _tieqi_good(card):
    return card.color == "red"


class TieqiFrame(Frame):
    def __init__(self, owner, ev):
        self.owner, self.ev = owner, ev

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(JudgeFrame(self.owner, "铁骑"))
        else:
            if g.cards[g.ret].color == "red":
                self.ev.info.no_dodge = True
            g.done()


@register_skill
class Jizhi(Skill):
    name = "集智"
    events = ("card_used",)
    description = "每当你使用一张非延时类锦囊时，（在它结算之前）你可以立即摸一张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.user == owner and CARDS[ev.card.name].type == "trick"

    def on_trigger(self, g, owner, event, ev):
        g.draw(owner, 1, "集智")


@register_skill
class Qicai(Skill):
    name = "奇才"
    description = "你使用任何锦囊无距离限制。"
    def ignore_distance(self, g, owner, user, vc):
        return CARDS[vc.name].is_trick


# ====================================================================== 吴
@register_skill
class Zhiheng(Skill):
    name = "制衡"
    description = "出牌阶段，你可以弃掉任意数量的牌，然后摸取等量的牌。每回合限用一次。"
    def can_activate(self, g, owner):
        return not _used(g, owner, self.name) and bool(_he(g, owner))

    def activate(self, g, owner):
        return ZhihengFrame(owner)


class ZhihengFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            cands = _he(g, self.owner)
            g.push(SelectCardsFrame(self.owner, cands, 1, len(cands), "制衡：选择要弃置的牌"))
        else:
            cards = g.ret
            g.to_discard(cards)
            g.log(f"{g.pname(self.owner)} 制衡弃置了 " + "、".join(g.cstr(c) for c in cards), "discard")
            g.draw(self.owner, len(cards), "制衡")
            g.done()


@register_skill
class Qixi(Skill):
    name = "奇袭"
    description = "出牌阶段，你可以将你的任意黑色牌当过河拆桥使用。"
    def view_as(self, g, owner, names, ctx):
        if names is not None and "过河拆桥" not in names:
            return []
        if ctx.get("mode") not in ("play", "use"):
            return []
        return _view(g, owner, [c for c in _he(g, owner) if g.cards[c].color == "black"], "过河拆桥", self.name)


@register_skill
class Keji(Skill):
    name = "克己"
    events = ("phase_discard",)
    description = "若你于出牌阶段未使用或打出过任何一张杀，你可以跳过此回合的弃牌阶段。"
    def can_trigger(self, g, owner, event, ev):
        p = g.players[owner]
        return (ev.seat == owner and not p.turn.get("slash_any")
                and len(p.hand) > g.max_hand(owner))

    def on_trigger(self, g, owner, event, ev):
        ev.skip.add("discard")


@register_skill
class Kurou(Skill):
    name = "苦肉"
    description = "出牌阶段，你可以失去 1 点体力，然后摸两张牌。每回合中，你可以多次使用苦肉。"
    def can_activate(self, g, owner):
        return g.players[owner].hp > 0

    def activate(self, g, owner):
        return KurouFrame(owner)


class KurouFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(LoseHpFrame(self.owner, 1))
        else:
            if g.players[self.owner].alive:
                g.draw(self.owner, 2, "苦肉")
            g.done()


@register_skill
class Yingzi(Skill):
    name = "英姿"
    events = ("draw_phase",)
    description = "摸牌阶段，你可以额外摸一张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner

    def on_trigger(self, g, owner, event, ev):
        ev.count += 1


@register_skill
class Fanjian(Skill):
    name = "反间"
    description = "出牌阶段，你可以令另一名角色选择一种花色，抽取你的一张手牌并亮出，若此牌与所选花色不吻合，则你对该角色造成 1 点伤害。然后不论结果，该角色都获得此牌。每回合限用一次。"

    def can_activate(self, g, owner):
        return not _used(g, owner, self.name) and bool(g.players[owner].hand) and bool(g.others(owner))

    def activate(self, g, owner):
        return FanjianFrame(owner)


class FanjianFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        st = self.stage
        if st == 0:
            self.stage = 1
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in g.others(self.owner)],
                               "反间：选择目标", {"skill": "反间"}))
        elif st == 1:
            self.target = g.ret[1]
            self.stage = 2
            g.push(ChoiceFrame(self.target, [(("suit", s), s) for s in SUITS], "反间：选择一种花色",
                               {"skill": "反间", "source": self.owner}))
        elif st == 2:
            suit = g.ret[1]
            hand = g.players[self.owner].hand
            if not hand:
                g.done()
                return
            cid = g.rng.choice(hand)
            g.to_hand(self.target, [cid], "反间", public=True)
            g.log(f"{g.pname(self.target)} 选择 {suit}，获得并展示了 {g.cstr(cid)}", "obtain")
            self.stage = 3
            if g.cards[cid].suit != suit:
                g.push(DamageFrame(self.owner, self.target, 1, None, "normal", "反间"))
        else:
            g.done()


@register_skill
class Guose(Skill):
    name = "国色"
    description = "出牌阶段，你可以将你的任意方块花色的牌当乐不思蜀使用。"
    def view_as(self, g, owner, names, ctx):
        if names is not None:
            return []
        return _view(g, owner, [c for c in _he(g, owner) if g.cards[c].suit == DIAMOND], "乐不思蜀", self.name)


@register_skill
class Liuli(Skill):
    name = "流离"
    events = ("target_confirm",)
    description = "当你成为杀的目标时，你可以弃一张牌，并将此杀转移给你攻击范围内的另一名角色（该角色不能是此杀的使用者）。"
    def _targets(self, g, owner, ev):
        return [t for t in g.others(owner) if t != ev.use.user and g.in_attack_range(owner, t)
                and t not in ev.use.info and not g.prohibited(ev.use.user, t, ev.use.card)]

    def can_trigger(self, g, owner, event, ev):
        return (ev.target == owner and ev.use.card.name == "杀" and not ev.info.cancelled
                and _he(g, owner) and self._targets(g, owner, ev))

    def make_frame(self, g, owner, event, ev):
        return LiuliFrame(owner, ev, self._targets(g, owner, ev))


class LiuliFrame(Frame):
    def __init__(self, owner, ev, targets):
        self.owner, self.ev, self.targets = owner, ev, targets

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(SelectCardsFrame(self.owner, _he(g, self.owner), 1, 1, "流离：弃置一张牌"))
        elif self.stage == 1:
            g.to_discard(g.ret)
            self.stage = 2
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in self.targets],
                               "流离：将杀转移给", {"skill": "流离"}))
        elif self.stage == 2:
            new = g.ret[1]
            use = self.ev.use
            self.ev.info.cancelled = True
            use.info[new] = Ev(shan=1, no_dodge=False, cancelled=False, extra=0, ignore_armor=False, duel_need={})
            g.log(f"{g.pname(self.owner)} 将杀转移给 {g.pname(new)}", "skill")
            self.stage = 3
            g.push(SlashEffect(use, new))
            g.push(TriggerFrame("target_confirm", Ev(use=use, target=new, info=use.info[new])))
        else:
            g.done()


@register_skill
class Qianxun(Skill):
    name = "谦逊"
    locked = True
    description = "锁定技，你不能成为顺手牵羊和乐不思蜀的目标。"
    def prohibit(self, g, owner, user, target, vc):
        return target == owner and vc.name in ("顺手牵羊", "乐不思蜀")


@register_skill
class Lianying(Skill):
    name = "连营"
    events = ("lose_card",)
    description = "每当你失去最后一张手牌时，可以立即摸一张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner and "h" in ev.zones and not g.players[owner].hand

    def on_trigger(self, g, owner, event, ev):
        g.draw(owner, 1, "连营")


@register_skill
class Jieyin(Skill):
    name = "结姻"
    description = "出牌阶段，你可以弃两张手牌并选择一名受伤的男性角色：你和目标角色各回复 1 点体力。每回合限用一次。"
    def _targets(self, g, owner):
        return [t for t in g.others(owner) if g.players[t].gender == "male" and g.players[t].wounded]

    def can_activate(self, g, owner):
        return not _used(g, owner, self.name) and len(g.players[owner].hand) >= 2 and bool(self._targets(g, owner))

    def activate(self, g, owner):
        return JieyinFrame(owner, self._targets(g, owner))


class JieyinFrame(Frame):
    def __init__(self, owner, targets):
        self.owner, self.targets = owner, targets

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in self.targets],
                               "结姻：选择目标", {"skill": "结姻"}))
        elif self.stage == 1:
            self.target = g.ret[1]
            self.stage = 2
            g.push(DiscardFrame(self.owner, 2, "h", "结姻", "结姻：弃置两张手牌"))
        else:
            g.recover(self.target, 1, self.owner)
            g.recover(self.owner, 1, self.owner)
            g.done()


@register_skill
class Xiaoji(Skill):
    name = "枭姬"
    events = ("lose_card",)
    description = "当你失去一张装备区里的牌时，你可以立即摸两张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner and "e" in ev.zones

    def on_trigger(self, g, owner, event, ev):
        for _ in range(ev.zones.count("e")):
            g.draw(owner, 2, "枭姬")


# ====================================================================== 群
@register_skill
class Wushuang(Skill):
    name = "无双"
    locked = True
    events = ("target_confirm",)
    description = "锁定技，你使用杀时，目标角色需连续使用两张闪才能抵消；与你进行决斗的角色每次需连续打出两张杀。"
    def can_trigger(self, g, owner, event, ev):
        n = ev.use.card.name
        if n == "杀":
            return ev.use.user == owner
        if n == "决斗":
            return ev.use.user == owner or ev.target == owner
        return False

    def on_trigger(self, g, owner, event, ev):
        if ev.use.card.name == "杀":
            ev.info.shan = max(ev.info.shan, 2)
        else:
            other = ev.target if ev.use.user == owner else ev.use.user
            ev.info.duel_need[other] = 2


@register_skill
class Jijiu(Skill):
    name = "急救"
    description = "你的回合外，你可以将你的任意红色牌当桃使用。"
    def view_as(self, g, owner, names, ctx):
        if names is None or "桃" not in names or _own_turn(g, owner):
            return []
        return _view(g, owner, [c for c in _he(g, owner) if g.cards[c].color == "red"], "桃", self.name)


@register_skill
class Qingnang(Skill):
    name = "青囊"
    description = "出牌阶段，你可以主动弃掉一张手牌，令任一目标角色回复 1 点体力。每回合限用一次。"
    def _targets(self, g, owner):
        return [t for t in g.alive_seats(owner) if g.players[t].wounded]

    def can_activate(self, g, owner):
        return not _used(g, owner, self.name) and bool(g.players[owner].hand) and bool(self._targets(g, owner))

    def activate(self, g, owner):
        return QingnangFrame(owner, self._targets(g, owner))


class QingnangFrame(Frame):
    def __init__(self, owner, targets):
        self.owner, self.targets = owner, targets

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(DiscardFrame(self.owner, 1, "h", "青囊", "青囊：弃置一张手牌"))
        elif self.stage == 1:
            self.stage = 2
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in self.targets],
                               "青囊：令谁回复体力", {"skill": "青囊"}))
        else:
            g.recover(g.ret[1], 1, self.owner)
            g.done()


@register_skill
class Lijian(Skill):
    name = "离间"
    description = "出牌阶段，你可以弃一张牌并选择两名男性角色，视为其中一名男性角色对另一名男性角色使用一张决斗（此决斗不能被无懈可击响应）。每回合限用一次。"
    def _males(self, g, owner):
        return [t for t in g.others(owner) if g.players[t].gender == "male"]

    def can_activate(self, g, owner):
        return not _used(g, owner, self.name) and bool(_he(g, owner)) and len(self._males(g, owner)) >= 2

    def activate(self, g, owner):
        return LijianFrame(owner)


class LijianFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        st = self.stage
        males = Lijian()._males(g, self.owner)
        if st == 0:
            self.stage = 1
            g.push(SelectCardsFrame(self.owner, _he(g, self.owner), 1, 1, "离间：弃置一张牌"))
        elif st == 1:
            g.to_discard(g.ret)
            self.stage = 2
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in males],
                               "离间：选择决斗的使用者", {"skill": "离间"}))
        elif st == 2:
            self.user = g.ret[1]
            self.stage = 3
            g.push(ChoiceFrame(self.owner, [(("target", t), g.pname(t)) for t in males if t != self.user],
                               "离间：选择决斗的目标", {"skill": "离间"}))
        elif st == 3:
            from ...core.actions import UseCardFrame
            self.stage = 4
            g.push(UseCardFrame(self.user, VCard("决斗", (), None, 0, "离间"), (g.ret[1],), {"no_nullify": True}))
        else:
            g.done()


@register_skill
class Biyue(Skill):
    name = "闭月"
    events = ("phase_finish",)
    description = "回合结束阶段，你可以摸一张牌。"
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner

    def on_trigger(self, g, owner, event, ev):
        g.draw(owner, 1, "闭月")


# ====================================================================== 主公技
# 主公技只有身份局的主公才有，1v1 两人对决里没有主公，所以这三个技能只登记说明、挂在武将的 lord_skills 上，
# 模式不会把它们发给玩家，也没有实现效果。以后写身份局时在这里补上效果即可（需要其他同势力角色参与）。
@register_skill
class Hujia(Skill):
    name = "护驾"
    lord = True
    description = ("主公技，当你需要使用（或打出）一张闪时，你可以发动护驾。所有魏势力角色按行动顺序依次选择是否打出一张闪"
                   "“提供”给你（视为由你使用或打出），直到有一名角色或没有任何角色决定如此做时为止。")


@register_skill
class Jijiang(Skill):
    name = "激将"
    lord = True
    description = ("主公技，当你需要使用（或打出）一张杀时，你可以发动激将。所有蜀势力角色按行动顺序依次选择是否打出一张杀"
                   "“提供”给你（视为由你使用或打出），直到有一名角色或没有任何角色决定如此做时为止。")


@register_skill
class Jiuyuan(Skill):
    name = "救援"
    lord = True
    locked = True
    description = "主公技，锁定技，其他吴势力角色在你濒死状态下对你使用桃时，你额外回复 1 点体力。"
