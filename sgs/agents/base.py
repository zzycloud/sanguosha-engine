"""智能体接口和随机智能体。"""
from __future__ import annotations

import random
from typing import Optional, Sequence

from ..core.game import Game


class Agent:
    """choose(g, decision) -> 选项下标。智能体只应使用 g.observe(decision.player) 能看到的信息；
    这里传入整个 g 是为了方便，规则 AI 自觉不看对手手牌和牌堆。"""

    def choose(self, g: Game, d) -> int:  # pragma: no cover
        raise NotImplementedError


class RandomAgent(Agent):
    def __init__(self, seed: Optional[int] = None, end_bias: float = 0.0):
        self.rng = random.Random(seed)
        self.end_bias = end_bias  # 出牌阶段直接结束的额外概率（0 表示纯随机）

    def choose(self, g, d):
        if d.kind == "play" and self.end_bias and self.rng.random() < self.end_bias:
            return len(d.options) - 1
        return self.rng.randrange(len(d.options))


def play_game(g: Game, agents: Sequence[Agent], max_steps: int = 100000) -> Game:
    n = 0
    while not g.over and n < max_steps:
        d = g.pending
        g.act(agents[d.player].choose(g, d))
        n += 1
    return g
