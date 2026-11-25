"""甄姬：弃牌阶段留黑杀（黑杀既能当杀，又能用倾国当闪），弃闪。"""
from __future__ import annotations

import random

from ..board import Board, end_play
from . import Scenario, register_scenario


@register_scenario
class ZhenjiDiscard(Scenario):
    name = "zhenji_discard"
    general = "甄姬"
    title = "甄姬弃牌留黑杀"
    question = "甄姬弃牌阶段要弃牌，手里有黑杀也有闪：先弃哪张？"
    counter = "手里还有红杀、桃等别的牌时，最该弃的可能是别的牌；黑牌已经很多时，多一张黑杀的价值也下降。"
    hero_seat = 0

    def build(self, seed):
        rng = random.Random(seed)
        opp = rng.choice(self.opponents())
        b = Board([self.general, opp], seed)
        b.to_play_phase(0)
        b.clear(0)
        b.clear(1)
        hp = self.pick(rng, [(1, 1), (2, 2), (3, 2)])
        b.hp(0, hp)
        b.hp(1, rng.randint(2, b.g.players[1].max_hp))
        over = self.pick(rng, [(1, 3), (2, 1)])        # 要弃几张
        b.give(0, "杀", color="black")
        b.give(0, "闪")
        rest = hp + over - 2
        red_slash = rest > 0 and rng.random() < 0.4
        if red_slash:
            b.give(0, "杀", color="red")
            rest -= 1
        if rest > 0:
            b.give_random(0, rest, exclude_names=("无懈可击",))
        b.give_random(1, rng.randint(1, 4))
        g = b.finish()
        b.until(lambda g: g.pending.player == 0 and g.pending.kind == "choose"
                and (g.pending.ctx or {}).get("reason") == "discard_phase" and not (g.pending.ctx or {}).get("forced"),
                [end_play(0)], limit=20)
        return g, {"hp": hp, "discard": over, "red_slash": red_slash, "opponent": opp}

    def tag(self, desc):
        return desc
