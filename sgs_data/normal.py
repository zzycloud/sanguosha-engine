"""普通局面：正常打一局，从里面随机抽几个“需要想”的决策点（跳过只有一个选项、或剩下的牌都得选的）。

它们占数据的大头，作用是让模型基本打法不丢：该杀就杀、该闪就闪、该出桃就出桃。
标签和关键局面一样靠往下模拟；平局题（选什么都差不多）直接扔掉。

配方 = 种子 + 武将 + 到这一步为止的动作序列，引擎是确定性的，照着重放就能回到同一个局面。
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence

from sgs.agents.base import auto_reason, decide
from sgs.core.game import Game

from .board import standard_generals


def new_game(seed: int, generals: Sequence[str]) -> Game:
    return Game("duel", seed=seed, generals=list(generals))


def replay(recipe: Dict[str, Any]) -> Game:
    g = new_game(recipe["seed"], recipe["generals"])
    for i in recipe["history"]:
        g.act(i)
    return g


def play_and_sample(seed: int, agents: Sequence[Any], per_game: int, pool: Optional[Sequence[str]] = None,
                    focus: Optional[Sequence[str]] = None, skip_kinds: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """打一局，返回抽到的决策点配方。focus：至少一方是这些武将之一（按武将分桶时用）。"""
    rng = random.Random(seed * 31 + 7)
    pool = list(pool or standard_generals())
    if focus:
        a = rng.choice(list(focus))
        b = rng.choice([x for x in pool if x != a])
        generals = [a, b] if rng.random() < 0.5 else [b, a]
    else:
        generals = rng.sample(pool, 2)
    g = new_game(seed, generals)
    cands: List[int] = []
    steps = 0
    while not g.over and steps < 20000:
        d = g.pending
        # 观星安排到一半的决策不抽：那几张牌还在牌堆里但已经被看过，重发牌堆会弄乱
        if auto_reason(d) is None and d.kind not in skip_kinds and d.ctx.get("skill") != "观星" and \
                (not focus or g.players[d.player].general in focus):
            cands.append(len(g.history))
        g.act(decide(agents[d.player], g))
        steps += 1
    picks = sorted(rng.sample(cands, min(per_game, len(cands))))
    hist = list(g.history)
    return [{"type": "game", "seed": seed, "generals": generals, "history": hist[:k]} for k in picks]
