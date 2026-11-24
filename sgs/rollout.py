"""rollout 估值：在一个决策点，对每个合法动作做若干次“确定化 + 模拟到终局”。

- determinize：把决策者看不到的牌（对手手牌 + 牌堆顺序）随机重排，不偷看隐藏信息；
- 所有动作在同一批确定化样本上比较（公共随机数），降低方差；
- 模拟策略默认是规则 AI，可以换成任意智能体。
"""
from __future__ import annotations

import random
from typing import Callable, List, Optional, Sequence

from .agents.base import Agent, play_game


def _rule_pair(k: int) -> Sequence[Agent]:
    from .agents.rule import RuleAgent
    return [RuleAgent(k), RuleAgent(k + 1)]


def evaluate(g, rollouts: int = 16, seed: int = 0,
             policy: Optional[Callable[[int], Sequence[Agent]]] = None) -> List[float]:
    """返回每个合法动作的期望得分（胜 1、平 0.5、负 0），从当前决策者的角度。"""
    d = g.pending
    me = d.player
    policy = policy or _rule_pair
    rng = random.Random(seed)
    worlds = []
    for _ in range(rollouts):
        w = g.determinize(me, random.Random(rng.random()))
        w.logging = False
        w.logs = []
        w.decisions = []
        worlds.append(w)
    scores = []
    for i in range(len(d.options)):
        total = 0.0
        for k, w in enumerate(worlds):
            c = w.clone()
            c.act(i)
            play_game(c, policy(k))
            total += 1.0 if c.winner == me else (0.5 if c.winner is None else 0.0)
        scores.append(total / rollouts)
    return scores
