"""司马懿：反馈拿什么。拿走连弩是自保（不让对面继续连杀）。"""
from __future__ import annotations

import random

from ..board import Board, answer_trigger, use_card
from . import Scenario, register_scenario


@register_scenario
class SimayiFankui(Scenario):
    name = "simayi_fankui"
    general = "司马懿"
    title = "司马懿反馈拿连弩"
    question = "司马懿被杀了一下发反馈：拿对面的诸葛连弩，还是拿手牌/别的装备？"
    counter = "对面是张飞（咆哮本来就能连杀，连弩没用）、对面没有连弩、或者对面手里已经没杀时，拿连弩不一定最好。"
    hero_seat = 1

    def build(self, seed):
        rng = random.Random(seed)
        opp = "张飞" if rng.random() < float(self.params.get("zhangfei_share", 0.2)) else \
            rng.choice(self.opponents(exclude=("张飞",)))
        b = Board([opp, self.general], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        b.hp(1, rng.randint(2, 3))
        b.hp(0, rng.randint(2, b.g.players[0].max_hp))
        weapon = self.pick(rng, [("诸葛连弩", 3), ("other", 1), ("none", 1)])
        if weapon == "诸葛连弩":
            b.equip(0, "诸葛连弩")
        elif weapon == "other":
            b.equip_random(0, ["weapon"], exclude_names=("诸葛连弩",))
        if rng.random() < 0.5:
            b.equip_random(0, [rng.choice(["armor", "plus_horse", "minus_horse"])])
        slashes = self.pick(rng, [(1, 1), (2, 2), (3, 1.5)])
        for _ in range(slashes):
            b.give(0, "杀")
        b.give_random(0, rng.randint(0, 2), exclude_names=("杀", "无懈可击"))
        b.give_random(1, rng.randint(0, 3), exclude_names=("闪",))
        g = b.finish()
        b.until(lambda g: g.pending.player == 1 and g.pending.kind == "choose"
                and (g.pending.ctx or {}).get("reason") == "反馈",
                [use_card(0, "杀", 1), answer_trigger(1, "反馈", True)], limit=80)
        return g, {"opponent": opp, "weapon": weapon, "slashes_left": slashes - 1}

    def tag(self, desc):
        if "诸葛连弩" in desc:
            return "拿连弩"
        if desc.startswith("武器"):
            return "拿武器"
        if "手牌" in desc:
            return "拿手牌"
        return "拿其它装备"
