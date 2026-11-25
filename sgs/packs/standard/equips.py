"""标准版装备牌。装备效果实现为“装备技能”，装上即拥有，失去即失去。"""
from itertools import combinations

from ...core.actions import (ChoiceFrame, DiscardFrame, JudgeFrame, SelectCardsFrame, UseCardFrame, can_slash,
                             card_options, judge_good, vc_option)
from ...core.base import Decision, Frame, Option
from ...core.cards import VCard
from ...core.plugin import INF, CardSpec, Skill
from ...core.registry import register_card, register_skill


class Equip(CardSpec):
    type = "equip"

    def play_targets(self, g, user, vc):
        return [(user,)]


def weapon(name_, rng, skill_=None):
    @register_card
    class _W(Equip):
        name = name_
        subtype = "weapon"
        weapon_range = rng
        skill = skill_
    _W.__name__ = f"Weapon_{name_}"
    return _W


def armor(name_, skill_):
    @register_card
    class _A(Equip):
        name = name_
        subtype = "armor"
        skill = skill_
    _A.__name__ = f"Armor_{name_}"
    return _A


def horse(name_, plus: bool):
    @register_card
    class _H(Equip):
        name = name_
        subtype = "plus_horse" if plus else "minus_horse"
    _H.__name__ = f"Horse_{name_}"
    return _H


class EquipSkill(Skill):
    equip = True


def _is_slash_by(ev, owner):
    return ev.use.user == owner and ev.use.card.name == "杀"


# ---------------------------------------------------------------- 武器
@register_skill
class Crossbow(EquipSkill):
    name = "诸葛连弩"
    locked = True
    description = "锁定技，出牌阶段你使用杀无次数限制。"

    def slash_limit_mod(self, g, owner):
        return INF


@register_skill
class QinggangSkill(EquipSkill):
    name = "青釭剑"
    locked = True
    events = ("target_confirm",)
    description = "锁定技，你使用的杀无视目标角色的防具。"

    def can_trigger(self, g, owner, event, ev):
        return _is_slash_by(ev, owner)

    def on_trigger(self, g, owner, event, ev):
        ev.info.ignore_armor = True


@register_skill
class DoubleSwordSkill(EquipSkill):
    name = "雌雄双股剑"
    events = ("target_confirm",)
    description = "你使用杀指定一名异性角色后，可以令其选择：弃置一张手牌，或令你摸一张牌。"

    def can_trigger(self, g, owner, event, ev):
        return _is_slash_by(ev, owner) and g.players[ev.target].gender != g.players[owner].gender

    def make_frame(self, g, owner, event, ev):
        return DoubleSwordFrame(owner, ev.target)


class DoubleSwordFrame(Frame):
    def __init__(self, owner, target):
        self.owner, self.target = owner, target

    def step(self, g):
        t = g.players[self.target]
        if self.stage == 0:
            if not t.hand:
                g.draw(self.owner, 1, "雌雄双股剑")
                g.done()
                return
            self.stage = 1
            g.push(ChoiceFrame(self.target, [(("discard",), "弃置一张手牌"),
                                             (("draw",), f"令 {g.pname(self.owner)} 摸一张牌")],
                               "雌雄双股剑：请选择", {"reason": "雌雄双股剑"}))
        elif self.stage == 1:
            self.stage = 2
            if g.ret == ("draw",):
                g.draw(self.owner, 1, "雌雄双股剑")
            else:
                g.push(DiscardFrame(self.target, 1, "h", "雌雄双股剑", "雌雄双股剑：弃置一张手牌"))
        else:
            g.done()


@register_skill
class IceSwordSkill(EquipSkill):
    name = "寒冰剑"
    events = ("damage_cause",)
    description = "你使用杀对目标造成伤害时，若其有牌，你可以防止此伤害，改为依次弃置其两张牌。"

    def can_trigger(self, g, owner, event, ev):
        return (ev.source == owner and ev.card is not None and ev.card.name == "杀" and ev.reason == "杀"
                and g.players[ev.target].card_count("he") > 0)

    def make_frame(self, g, owner, event, ev):
        ev.prevented = True
        return IceFrame(owner, ev.target)


class IceFrame(Frame):
    def __init__(self, owner, target):
        self.owner, self.target, self.n = owner, target, 0

    def step(self, g):
        from .tricks import TakeCardFrame
        if self.n < 2 and g.players[self.target].card_count("he") > 0:
            self.n += 1
            g.push(TakeCardFrame(self.owner, self.target, True, "he", "寒冰剑"))
        else:
            g.done()


@register_skill
class BladeSkill(EquipSkill):
    name = "青龙偃月刀"
    events = ("slash_dodged",)
    description = "你使用的杀被闪抵消后，可以对该角色再使用一张杀。"

    def can_trigger(self, g, owner, event, ev):
        return ev.use.user == owner and g.players[ev.target].alive and bool(
            card_options(g, owner, ("杀",), {"mode": "use"}))

    def make_frame(self, g, owner, event, ev):
        return BladeFrame(owner, ev.target)


class BladeFrame(Frame):
    def __init__(self, owner, target):
        self.owner, self.target = owner, target

    def step(self, g):
        if self.stage == 0:
            vcs = [vc for vc in card_options(g, self.owner, ("杀",), {"mode": "use"})
                   if can_slash(g, self.owner, self.target, vc, check_range=False)]
            opts = [vc_option(g, vc, (self.target,), "青龙偃月刀：使用 ") for vc in vcs]
            if not opts:
                g.done()
                return
            self.stage = 1
            g.ask(Decision(self.owner, "respond", "青龙偃月刀：选择再使用哪张杀", opts,
                           {"names": ["杀"], "skill": "青龙偃月刀", "target": self.target}))
        elif self.stage == 1:
            ans = g.answer()
            self.stage = 2
            if ans.key != ("pass",):
                g.push(UseCardFrame(self.owner, ans.data, (self.target,), {"extra_slash": True}))
        else:
            g.done()


@register_skill
class SpearSkill(EquipSkill):
    name = "丈八蛇矛"
    description = "你可以将两张手牌当杀使用或打出。"

    def view_as(self, g, owner, names, ctx):
        if names is not None and "杀" not in names:
            return []
        hand = g.players[owner].hand
        return [VCard.convert(g, "杀", pair, self.name) for pair in combinations(sorted(hand), 2)]


@register_skill
class AxeSkill(EquipSkill):
    name = "贯石斧"
    events = ("slash_dodged",)
    description = "你使用的杀被闪抵消后，可以弃置两张牌（装备区的贯石斧除外），令此杀依然造成伤害。"

    def _cands(self, g, owner):
        p = g.players[owner]
        return list(p.hand) + [c for s, c in p.equips.items() if s != "weapon"]

    def can_trigger(self, g, owner, event, ev):
        return ev.use.user == owner and len(self._cands(g, owner)) >= 2

    def make_frame(self, g, owner, event, ev):
        return AxeFrame(owner, ev, self._cands(g, owner))


class AxeFrame(Frame):
    def __init__(self, owner, ev, cands):
        self.owner, self.ev, self.cands = owner, ev, cands

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(SelectCardsFrame(self.owner, self.cands, 2, 2, "贯石斧：弃置两张牌令杀强制命中",
                                    {"reason": "贯石斧"}))
        else:
            g.to_discard(g.ret)
            g.log(f"{g.pname(self.owner)} 弃置 " + "、".join(g.cstr(c) for c in g.ret) + " 令杀强制命中", "discard")
            self.ev.hit = True
            g.done()


@register_skill
class BowSkill(EquipSkill):
    name = "麒麟弓"
    events = ("damage_cause",)
    description = "你使用杀对目标造成伤害时，可以弃置其装备区里的一张坐骑牌。"

    def can_trigger(self, g, owner, event, ev):
        if not (ev.source == owner and ev.card is not None and ev.card.name == "杀" and ev.reason == "杀"):
            return False
        e = g.players[ev.target].equips
        return "plus_horse" in e or "minus_horse" in e

    def make_frame(self, g, owner, event, ev):
        e = g.players[ev.target].equips
        horses = [e[s] for s in ("plus_horse", "minus_horse") if s in e]
        return BowFrame(owner, horses)


class BowFrame(Frame):
    def __init__(self, owner, horses):
        self.owner, self.horses = owner, horses

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.push(SelectCardsFrame(self.owner, self.horses, 1, 1, "麒麟弓：弃置一张坐骑牌"))
        else:
            g.to_discard(g.ret)
            g.log(f"{g.pname(self.owner)} 用麒麟弓弃置了 {g.cstr(g.ret[0])}", "discard")
            g.done()


# ---------------------------------------------------------------- 防具
@register_skill
class EightDiagramSkill(EquipSkill):
    name = "八卦阵"
    description = "当你需要使用或打出闪时，可以进行判定：若为红色，视为你使用或打出了一张闪。"

    def response_options(self, g, owner, names, ctx):
        if "闪" in names and self.armor_active(g, owner, ctx):
            return ["发动【八卦阵】判定"]
        return []

    def response_frame(self, g, owner, names, ctx):
        return BaguaFrame(owner)


@judge_good("八卦阵")
def _bagua_good(card):
    return card.color == "red"


class BaguaFrame(Frame):
    def __init__(self, owner):
        self.owner = owner

    def step(self, g):
        if self.stage == 0:
            self.stage = 1
            g.log(f"{g.pname(self.owner)} 发动了【八卦阵】", "skill")
            g.push(JudgeFrame(self.owner, "八卦阵"))
        else:
            if g.cards[g.ret].color == "red":
                g.log(f"{g.pname(self.owner)} 的八卦阵判定成功，视为使用了闪", "info")
                g.done(VCard("闪", (), None, 0, "八卦阵"))
            else:
                g.done(None)


@register_skill
class RenwangSkill(EquipSkill):
    name = "仁王盾"
    locked = True
    events = ("target_confirm",)
    description = "锁定技，黑色的杀对你无效。"

    def can_trigger(self, g, owner, event, ev):
        return (ev.target == owner and ev.use.card.name == "杀" and ev.use.card.color == "black"
                and not ev.info.ignore_armor)

    def on_trigger(self, g, owner, event, ev):
        ev.info.cancelled = True
        g.log(f"仁王盾令黑色杀对 {g.pname(owner)} 无效", "info")


# ---------------------------------------------------------------- 牌
weapon("诸葛连弩", 1, "诸葛连弩")
weapon("青釭剑", 2, "青釭剑")
weapon("雌雄双股剑", 2, "雌雄双股剑")
weapon("寒冰剑", 2, "寒冰剑")
weapon("青龙偃月刀", 3, "青龙偃月刀")
weapon("丈八蛇矛", 3, "丈八蛇矛")
weapon("贯石斧", 3, "贯石斧")
weapon("方天画戟", 4, None)  # 额外目标效果在 1v1 无意义，暂未实现（见 docs/rules.md）
weapon("麒麟弓", 5, "麒麟弓")
armor("八卦阵", "八卦阵")
armor("仁王盾", "仁王盾")
for _n in ("绝影", "的卢", "爪黄飞电"):
    horse(_n, True)
for _n in ("赤兔", "大宛", "紫骍"):
    horse(_n, False)
