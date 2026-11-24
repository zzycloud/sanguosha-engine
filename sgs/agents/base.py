"""智能体接口和随机智能体。"""
from __future__ import annotations

import random
from typing import Optional, Sequence

from ..core.game import Game


class Agent:
    """choose(view, decision) -> 选项下标。

    view 是 PlayerView（sgs/core/view.py）：决策者此刻能看到的信息快照，不含 Game 的引用，
    看不到对手手牌、牌堆顺序和别人的私密日志。引擎通过 decide() 调用智能体，智能体拿不到完整状态。
    """

    needs_view = True  # 不看局面的智能体（如随机）设为 False，省掉生成视角的开销

    def choose(self, view, d) -> int:  # pragma: no cover
        raise NotImplementedError


def decide(agent: Agent, g: Game) -> int:
    """让 agent 在 g 的当前决策点做选择：只把决策者的视角交给它。"""
    d = g.pending
    return agent.choose(g.view(d.player) if agent.needs_view else None, d)


class RandomAgent(Agent):
    needs_view = False
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
        g.act(decide(agents[d.player], g))
        n += 1
    return g
