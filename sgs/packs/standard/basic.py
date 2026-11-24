"""基本牌：杀、闪、桃。"""
from ...core.actions import (AskForCardFrame, DamageFrame, can_slash, trigger)
from ...core.base import Ev, Frame
from ...core.plugin import CardSpec
from ...core.registry import register_card


@register_card
class Slash(CardSpec):
    name = "杀"
    type = "basic"

    def play_targets(self, g, user, vc):
        p = g.players[user]
        if p.turn.get("slash", 0) >= g.slash_limit(user):
            return []
        return [(t,) for t in g.others(user) if can_slash(g, user, t, vc)]

    def effect(self, g, use, target):
        return SlashEffect(use, target)


class SlashEffect(Frame):
    """杀对一个目标的结算：要闪（无双要两张）→ 被抵消时触发 slash_dodged（青龙、贯石斧）→ 命中造成伤害。"""

    def __init__(self, use, target):
        self.use, self.target = use, target

    def step(self, g):
        use, t = self.use, self.target
        info = use.info[t]
        st = self.stage
        if st == 0:
            self.got = 0
            self.stage = 3 if info.no_dodge else 1
            if info.no_dodge:
                g.log(f"{g.pname(t)} 不能使用闪响应此杀", "info")
        if self.stage == 1:
            if self.got >= info.shan:
                self.stage = 4
            else:
                self.stage = 2
                need = f"（还需 {info.shan - self.got} 张）" if info.shan > 1 else ""
                g.push(AskForCardFrame(t, ("闪",), {"source": use.user, "slash": True,
                                                    "ignore_armor": info.ignore_armor}, True,
                                       f"{g.pname(use.user)} 对你使用了杀，请使用一张闪{need}"))
                return
        if self.stage == 2:
            if g.ret is None:
                self.stage = 3
            else:
                self.got += 1
                self.stage = 1
            return
        if self.stage == 3:
            self.stage = 9
            g.push(DamageFrame(use.user, t, 1 + info.extra, use.card, "normal", "杀"))
            return
        if self.stage == 4:
            g.log(f"{g.pname(use.user)} 的杀被抵消", "info")
            self.ev = Ev(use=use, target=t, info=info, hit=False)
            self.stage = 5
            trigger(g, "slash_dodged", self.ev)
            return
        if self.stage == 5:
            if self.ev.hit:
                self.stage = 3
            else:
                g.done()
            return
        g.done()


@register_card
class Dodge(CardSpec):
    name = "闪"
    type = "basic"
    response_only = True


@register_card
class Peach(CardSpec):
    name = "桃"
    type = "basic"

    def play_targets(self, g, user, vc):
        return [(user,)] if g.players[user].wounded else []

    def effect(self, g, use, target):
        g.recover(target, 1, use.user)
        return None
