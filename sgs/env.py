"""类 gym 的环境接口。

    env = SgsEnv(seed=1, generals=["关羽", "吕布"])
    obs = env.reset()
    while not env.done:
        seat = env.current_player()
        actions = env.legal_actions()          # list[Option]，option.label 是中文描述
        obs, rewards, done, info = env.step(0)  # 选第 0 个

rewards 是长度为玩家数的列表：胜者 +1，败者 -1，平局 0，未结束全为 0。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core.game import Game


class SgsEnv:
    def __init__(self, mode: str = "duel", seed: int = 0, **kw):
        self.mode = mode
        self.seed = seed
        self.kw = kw
        self.game: Optional[Game] = None

    def reset(self, seed: Optional[int] = None) -> Dict[str, Any]:
        if seed is not None:
            self.seed = seed
        self.game = Game(self.mode, seed=self.seed, **self.kw)
        return self.observe()

    @property
    def done(self) -> bool:
        return self.game.over

    def current_player(self) -> Optional[int]:
        d = self.game.pending
        return None if d is None else d.player

    def legal_actions(self):
        return self.game.legal_actions()

    def observe(self, seat: Optional[int] = -1) -> Dict[str, Any]:
        """默认是当前决策者视角；seat=None 是上帝视角。"""
        if seat == -1:
            seat = self.current_player()
        return self.game.observe(seat)

    def rewards(self) -> List[float]:
        g = self.game
        n = len(g.players)
        if not g.over or g.winner is None:
            return [0.0] * n
        return [1.0 if i == g.winner else -1.0 for i in range(n)]

    def step(self, index: int):
        self.game.act(index)
        return self.observe(), self.rewards(), self.done, {"winner": self.game.winner, "result": self.game.result}

    def clone(self) -> "SgsEnv":
        e = SgsEnv.__new__(SgsEnv)
        e.mode, e.seed, e.kw = self.mode, self.seed, self.kw
        e.game = self.game.clone()
        return e
