"""郭嘉：卖血（被杀时有闪也不出，吃伤害发遗计摸牌）、遗计给牌破空城。"""
from __future__ import annotations

import random

from ..board import Board, BuildFailed, answer_trigger, use_card
from . import Scenario, register_scenario


@register_scenario
class GuojiaSellHp(Scenario):
    name = "guojia_sell_hp"
    general = "郭嘉"
    title = "郭嘉卖血"
    question = "对手用杀打郭嘉，手里有闪：出闪，还是吃这一下发遗计摸两张？"
    counter = "郭嘉只剩 1 血、或者 2 血手里没桃时，卖血可能直接送命；对手是吕布（要两张闪）时情况也不同。"
    hero_seat = 1

    def build(self, seed):
        rng = random.Random(seed)
        opp = rng.choice(self.opponents())
        b = Board([opp, self.general], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        hp = self.pick(rng, [(1, 1), (2, 1.5), (3, 2)])
        b.hp(1, hp)
        b.hp(0, rng.randint(2, b.g.players[0].max_hp))
        n_shan = self.pick(rng, [(1, 2), (2, 1)])
        for _ in range(n_shan):
            b.give(1, "闪")
        peach = rng.random() < 0.35
        if peach:
            b.give(1, "桃")
        b.give_random(1, rng.randint(0, 2), exclude_names=("闪", "桃"))
        b.give(0, "杀")
        b.give_random(0, rng.randint(0, 3), exclude_names=("无懈可击",))
        if rng.random() < 0.4:
            b.equip_random(1, [rng.choice(["armor", "plus_horse", "minus_horse"])], exclude_names=("八卦阵",))
        g = b.finish()
        b.until(lambda g: g.pending.player == 1 and g.pending.kind == "respond"
                and "闪" in (g.pending.ctx or {}).get("names", []) and (g.pending.ctx or {}).get("slash"),
                [use_card(0, "杀", 1)], limit=60)
        return g, {"hp": hp, "shan": n_shan, "peach": peach, "opponent": opp}

    def tag(self, desc):
        return "不出" if desc == "不出" else "出闪"


@register_scenario
class GuojiaYijiKongcheng(Scenario):
    name = "guojia_yiji_kongcheng"
    general = "郭嘉"
    title = "遗计给牌破空城"
    question = "郭嘉被诸葛亮打了发遗计，摸到的牌：自己留着，还是塞一张给诸葛亮破他的空城？"
    counter = "诸葛亮手里本来还有牌（没在空城）时，给牌就是白送。"
    hero_seat = 1

    def build(self, seed):
        rng = random.Random(seed)
        b = Board(["诸葛亮", self.general], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        b.hp(1, rng.randint(2, 3))
        b.hp(0, self.pick(rng, [(1, 1), (2, 1), (3, 1)]))
        extra = self.pick(rng, [(0, 3), (1, 1.5), (2, 1)])   # 0：打完杀就空城；1、2：反例
        b.give(0, "杀")
        if extra:
            b.give_random(0, extra, exclude_names=("杀", "无懈可击"), only_types=("basic", "trick"))
        b.give_random(1, rng.randint(0, 3), exclude_names=("闪",))
        g = b.finish()
        b.until(lambda g: g.pending.player == 1 and g.pending.kind == "choose"
                and (g.pending.ctx or {}).get("skill") == "遗计",
                [use_card(0, "杀", 1), answer_trigger(1, "遗计", True)], limit=80)
        if len(g.players[0].hand) != extra:
            raise BuildFailed("诸葛亮的手牌数不对")
        return g, {"zhuge_hand": extra, "kongcheng": extra == 0, "card": g.cards[g.pending.ctx["card"]].name}

    def tag(self, desc):
        return desc
