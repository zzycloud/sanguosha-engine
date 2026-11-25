"""标准版锦囊牌（含延时锦囊）。"""
from ...core.actions import (AskForCardFrame, ChooseCardFrame, DamageFrame, JudgeFrame, NullifyFrame,
                             UseCardFrame, can_slash, card_options, judge_good, vc_option)
from ...core.base import Decision, Frame, Option
from ...core.cards import HEART, SPADE
from ...core.plugin import CardSpec
from ...core.registry import register_card


class Trick(CardSpec):
    type = "trick"

    def _ok(self, g, user, t, vc):
        return g.players[t].alive and not g.prohibited(user, t, vc)


# ---------------------------------------------------------------- 单目标
@register_card
class Duel(Trick):
    name = "决斗"

    def play_targets(self, g, user, vc):
        return [(t,) for t in g.others(user) if self._ok(g, user, t, vc)]

    def effect(self, g, use, target):
        return DuelFrame(use, target)


class DuelFrame(Frame):
    """决斗：由目标开始轮流打出杀，先不出的一方受到对方 1 点伤害。无双令对方每次需打出两张。"""

    def __init__(self, use, target):
        self.use, self.target = use, target

    def step(self, g):
        use, info = self.use, self.use.info[self.target]
        if self.stage == 0:
            self.cur, self.opp, self.k = self.target, use.user, 0
            self.stage = 1
        if self.stage == 1:
            if self.k >= info.duel_need.get(self.cur, 1):
                self.cur, self.opp, self.k = self.opp, self.cur, 0
            if not g.players[self.cur].alive or not g.players[self.opp].alive:
                g.done()
                return
            self.stage = 2
            g.push(AskForCardFrame(self.cur, ("杀",), {"source": self.opp, "duel": True}, False,
                                   f"决斗：请打出一张杀，否则受到 {g.pname(self.opp)} 的 1 点伤害"))
        elif self.stage == 2:
            if g.ret is None:
                self.stage = 3
                g.push(DamageFrame(self.opp, self.cur, 1, use.card, "normal", "决斗"))
            else:
                self.k += 1
                self.stage = 1
        else:
            g.done()


@register_card
class Dismantle(Trick):
    name = "过河拆桥"

    def play_targets(self, g, user, vc):
        return [(t,) for t in g.others(user) if self._ok(g, user, t, vc) and g.players[t].card_count("hej")]

    def no_effect(self, g, use, target):
        return None if g.players[target].card_count("hej") else "区域里没有牌"

    def effect(self, g, use, target):
        return TakeCardFrame(use.user, target, discard=True)


@register_card
class Snatch(Trick):
    name = "顺手牵羊"

    def play_targets(self, g, user, vc):
        """距离为 1 的其他角色（看坐骑，不看武器的攻击范围；奇才无视距离），而且区域里要有牌；谦逊不能成为目标。"""
        out = []
        for t in g.others(user):
            with g.unequipped(user, vc.subcards):
                near = g.distance(user, t) <= 1 or g.ignores_distance(user, vc)
            if not near:
                continue
            if not self._ok(g, user, t, vc) or not g.players[t].card_count("hej"):
                continue
            out.append((t,))
        return out

    def no_effect(self, g, use, target):
        return None if g.players[target].card_count("hej") else "区域里没有牌"

    def effect(self, g, use, target):
        return TakeCardFrame(use.user, target, discard=False)


class TakeCardFrame(Frame):
    """选择目标区域内一张牌，弃置（拆）或获得（顺）。"""

    def __init__(self, user, target, discard: bool, zones: str = "hej", reason: str = ""):
        self.user, self.target, self.discard, self.zones = user, target, discard, zones
        self.reason = reason or ("过河拆桥" if discard else "顺手牵羊")

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            verb = "弃置" if self.discard else "获得"
            g.push(ChooseCardFrame(self.user, self.target, self.zones,
                                   f"选择 {g.pname(self.target)} 的一张牌{verb}", self.reason))
            return
        cid = g.ret
        if cid is not None:
            if self.discard:
                g.to_discard([cid])
                g.log(f"{g.pname(self.user)} 弃置了 {g.pname(self.target)} 的 {g.cstr(cid)}", "discard")
            else:
                zone, _ = g.locate(cid)
                g.to_hand(self.user, [cid], self.reason)
                shown = g.cstr(cid) if zone != "h" else "一张手牌"
                g.log(f"{g.pname(self.user)} 获得了 {g.pname(self.target)} 的{shown}", "obtain",
                      private={self.user: g.cstr(cid), self.target: g.cstr(cid)} if zone == "h" else None)
        g.done(cid)


@register_card
class ExNihilo(Trick):
    name = "无中生有"

    def play_targets(self, g, user, vc):
        return [(user,)] if not g.prohibited(user, user, vc) else []

    def effect(self, g, use, target):
        g.draw(target, 2, "无中生有")


@register_card
class Collateral(Trick):
    name = "借刀杀人"

    def play_targets(self, g, user, vc):
        out = []
        for a in g.others(user):
            if "weapon" not in g.players[a].equips or not self._ok(g, user, a, vc):
                continue
            from ...core.cards import VCard
            probe = VCard("杀")
            for b in g.alive_seats(a):
                if b != a and can_slash(g, a, b, probe):
                    out.append((a, b))
        return out

    def split_targets(self, targets):
        return [targets[0]], {"victim": targets[1]}

    def effect(self, g, use, target):
        return CollateralFrame(use, target)


class CollateralFrame(Frame):
    def __init__(self, use, target):
        self.use, self.target = use, target

    def step(self, g):
        a, b, user = self.target, self.use.victim, self.use.user
        if self.stage == 0:
            if not g.players[b].alive or "weapon" not in g.players[a].equips:
                g.done()
                return
            vcs = [vc for vc in card_options(g, a, ("杀",), {"mode": "use"}) if can_slash(g, a, b, vc)]
            opts = [vc_option(g, vc, (b,), "使用 ") for vc in vcs]
            w = g.players[a].equips["weapon"]
            opts.append(Option(("give",), f"把武器 {g.cstr(w)} 交给 {g.pname(user)}"))
            self.stage = 1
            g.ask(Decision(a, "respond", f"借刀杀人：对 {g.pname(b)} 使用一张杀，或交出武器", opts,
                           {"names": ["杀"], "collateral": True, "user": user, "victim": b}))
        elif self.stage == 1:
            ans = g.answer()
            self.stage = 2
            if ans.key == ("give",):
                w = g.players[a].equips["weapon"]
                g.to_hand(user, [w], "借刀杀人")
                g.log(f"{g.pname(a)} 将 {g.cstr(w)} 交给了 {g.pname(user)}", "obtain")
            else:
                g.push(UseCardFrame(a, ans.data, (b,), {"extra_slash": True}))
        else:
            g.done()


# ---------------------------------------------------------------- 群体
class AOE(Trick):
    ask_name = "杀"
    used = False

    def play_targets(self, g, user, vc):
        ts = tuple(t for t in g.others(user) if self._ok(g, user, t, vc))
        return [ts] if ts else []

    def effect(self, g, use, target):
        return AOEFrame(use, target, self.ask_name, self.used, self.name)


class AOEFrame(Frame):
    def __init__(self, use, target, name, used, trick):
        self.use, self.target, self.name, self.used, self.trick = use, target, name, used, trick

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            verb = "使用" if self.used else "打出"
            g.push(AskForCardFrame(self.target, (self.name,), {"source": self.use.user, "aoe": self.trick},
                                   self.used, f"{self.trick}：请{verb}一张{self.name}，否则受到 1 点伤害"))
        elif self.stage == 1:
            self.stage = 2
            if g.ret is None:
                g.push(DamageFrame(self.use.user, self.target, 1, self.use.card, "normal", self.trick))
        else:
            g.done()


@register_card
class SavageAssault(AOE):
    name = "南蛮入侵"
    ask_name = "杀"
    used = False


@register_card
class ArcheryAttack(AOE):
    name = "万箭齐发"
    ask_name = "闪"
    used = True


@register_card
class GodSalvation(Trick):
    name = "桃园结义"

    def play_targets(self, g, user, vc):
        # 目标是所有已受伤的角色：体力已满的角色不是目标，也就不会被询问无懈可击。没有人受伤时不能使用
        ts = tuple(t for t in g.alive_seats(user) if g.players[t].wounded and not g.prohibited(user, t, vc))
        return [ts] if ts else []

    def effect(self, g, use, target):
        g.recover(target, 1, use.user)


@register_card
class AmazingGrace(Trick):
    name = "五谷丰登"

    def play_targets(self, g, user, vc):
        ts = tuple(t for t in g.alive_seats(user) if not g.prohibited(user, t, vc))
        return [ts] if ts else []

    def before_effects(self, g, use):
        n = len(use.targets)
        cards = [g.draw_top() for _ in range(n)]
        g.processing.extend(cards)
        use.extra.extend(cards)
        g.log("五谷丰登亮出：" + "、".join(g.cstr(c) for c in cards), "reveal")
        return None

    def effect(self, g, use, target):
        return GraceFrame(use, target)


class GraceFrame(Frame):
    def __init__(self, use, target):
        self.use, self.target = use, target

    def step(self, g):
        left = [c for c in self.use.extra if c in g.processing]
        if self.stage == 0:
            if not left:
                g.done()
                return
            self.stage = 1
            g.ask(Decision(self.target, "choose", "五谷丰登：选择一张牌获得",
                           [Option(("card", c), g.cstr(c)) for c in left], {"reason": "五谷丰登"}))
        else:
            cid = g.answer().key[1]
            g.to_hand(self.target, [cid], "五谷丰登", public=True)
            g.log(f"{g.pname(self.target)} 获得了 {g.cstr(cid)}", "obtain")
            g.done()


@register_card
class Nullification(Trick):
    name = "无懈可击"
    response_only = True


# ---------------------------------------------------------------- 延时锦囊
class Delayed(CardSpec):
    type = "delayed"

    def has_same(self, g, seat):
        return any(n == self.name for _, n in g.players[seat].judges)


@register_card
class Indulgence(Delayed):
    name = "乐不思蜀"

    def play_targets(self, g, user, vc):
        return [(t,) for t in g.others(user)
                if not self.has_same(g, t) and not g.prohibited(user, t, vc)]

    def delayed_effect(self, g, owner, cid, as_name):
        return IndulgenceFrame(owner, cid)


@judge_good("乐不思蜀")
def _indulgence_good(card):
    return card.suit == HEART


class IndulgenceFrame(Frame):
    def __init__(self, owner, cid):
        self.owner, self.cid = owner, cid

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(NullifyFrame("乐不思蜀", None, self.owner))
        elif self.stage == 1:
            if g.ret:
                g.clear_processing([self.cid])
                g.done()
                return
            self.stage = 2
            g.push(JudgeFrame(self.owner, "乐不思蜀"))
        else:
            card = g.cards[g.ret]
            g.clear_processing([self.cid])
            if card.suit != HEART:
                g.players[self.owner].turn.setdefault("skip", set()).add("play")
                g.log(f"{g.pname(self.owner)} 的【乐不思蜀】生效，将跳过出牌阶段", "info")
            else:
                g.log(f"{g.pname(self.owner)} 的【乐不思蜀】判定为红桃，没有生效", "info")
            g.done()


@register_card
class Lightning(Delayed):
    name = "闪电"

    def play_targets(self, g, user, vc):
        return [(user,)] if not self.has_same(g, user) and not g.prohibited(user, user, vc) else []

    def delayed_effect(self, g, owner, cid, as_name):
        return LightningFrame(owner, cid)


@judge_good("闪电")
def _lightning_good(card):
    return not (card.suit == SPADE and 2 <= card.rank <= 9)


class LightningFrame(Frame):
    def __init__(self, owner, cid):
        self.owner, self.cid = owner, cid

    def _move_on(self, g):
        g.processing.remove(self.cid)
        s = self.owner
        for _ in range(len(g.players)):
            s = g.next_alive(s)
            p = g.players[s]
            if not any(n == "闪电" for _, n in p.judges):
                p.judges.append((self.cid, "闪电"))
                if s != self.owner:
                    g.log(f"【闪电】没有生效，移动到 {g.pname(s)} 的判定区", "info")
                else:
                    g.log(f"【闪电】没有生效，留在 {g.pname(s)} 的判定区", "info")
                return
        g.players[self.owner].judges.append((self.cid, "闪电"))
        g.log(f"【闪电】没有生效，留在 {g.pname(self.owner)} 的判定区", "info")

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(NullifyFrame("闪电", None, self.owner))
        elif self.stage == 1:
            if g.ret:
                self._move_on(g)
                g.done()
                return
            self.stage = 2
            g.push(JudgeFrame(self.owner, "闪电"))
        elif self.stage == 2:
            card = g.cards[g.ret]
            if card.suit == SPADE and 2 <= card.rank <= 9:
                g.clear_processing([self.cid])
                self.stage = 3
                g.push(DamageFrame(None, self.owner, 3, None, "thunder", "闪电"))
            else:
                self._move_on(g)
                g.done()
        else:
            g.done()
