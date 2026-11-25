"""诸葛亮：残血凑空城、放不放闪电（对面是司马懿时例外）。"""
from __future__ import annotations

import random

from ..board import Board
from . import Scenario, register_scenario

USABLE = ("杀", "过河拆桥", "顺手牵羊", "无中生有", "决斗", "南蛮入侵", "万箭齐发")


@register_scenario
class ZhugeKongcheng(Scenario):
    name = "zhuge_kongcheng"
    general = "诸葛亮"
    title = "诸葛亮凑空城"
    question = "诸葛亮出牌阶段手里只剩几张牌：把能打的都打出去进空城，还是留着？"
    counter = "满血时、或者手里有打不出去的闪/桃时，硬凑空城不一定值；无中生有会摸牌，打了反而空不了。"
    hero_seat = 0

    def build(self, seed):
        rng = random.Random(seed)
        opp = rng.choice(self.opponents())
        b = Board([self.general, opp], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        hp = self.pick(rng, [(1, 2), (2, 2), (3, 1)])
        b.hp(0, hp)
        b.hp(1, rng.randint(2, b.g.players[1].max_hp))
        n = self.pick(rng, [(1, 2), (2, 2), (3, 1)])
        kinds = []
        for _ in range(n):
            k = self.pick(rng, [("equip", 2), ("usable", 3), ("keep", 1.2)])
            kinds.append(k)
            if k == "equip":
                b.give_random(0, 1, only_types=("equip",))
            elif k == "usable":
                b.give(0, rng.choice(USABLE))
            else:
                b.give(0, rng.choice(["闪", "桃", "无懈可击"]))
        b.give_random(1, rng.randint(1, 4))
        if rng.random() < 0.3:
            b.equip_random(1, [rng.choice(["weapon", "armor"])])
        g = b.finish()
        return g, {"hp": hp, "hand": n, "kinds": kinds, "opponent": opp,
                   "can_empty": "keep" not in kinds}

    def tag(self, desc):
        return "结束出牌" if desc == "结束出牌阶段" else "继续出牌"


@register_scenario
class ZhugeLightning(Scenario):
    name = "zhuge_lightning"
    general = "诸葛亮"
    title = "诸葛亮放闪电"
    question = "诸葛亮手里有闪电：现在放不放？（观星能把自己的判定牌换成安全的）"
    counter = "对面是司马懿时，鬼才能把诸葛亮的闪电判定改成黑桃 2-9，放闪电等于给他送伤害。"
    hero_seat = 0

    def build(self, seed):
        rng = random.Random(seed)
        opp = "司马懿" if rng.random() < float(self.params.get("simayi_share", 0.4)) else \
            rng.choice(self.opponents(exclude=("司马懿",)))
        b = Board([self.general, opp], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        b.hp(0, rng.randint(1, 3))
        b.hp(1, rng.randint(2, b.g.players[1].max_hp))
        b.give(0, "闪电")
        b.give_random(0, rng.randint(1, 3), exclude_names=("闪电",))
        b.give_random(1, rng.randint(1, 4))
        g = b.finish()
        return g, {"opponent": opp, "simayi": opp == "司马懿", "hp": g.players[0].hp}

    def tag(self, desc):
        return "放闪电" if "闪电" in desc else "不放"
