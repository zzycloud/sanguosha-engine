"""智能体接口、随机智能体和对局循环。

所有对手（随机 AI、规则 AI、大模型、你自己的策略服务）都实现同一个接口：

    class Agent:
        def choose(self, view, d) -> int     # 返回 d.options 的下标

- view：PlayerView（sgs/core/view.py），决策者此刻能看到的全部信息，看不到对手手牌和牌堆；
- d：Decision，d.kind / d.prompt / d.options[i].key（动作编码）/ d.options[i].label（中文描述）/ d.ctx。

可选的生命周期钩子：on_game_start(info) 每局开始时调用，info 有 game_id、seat、seed、generals；
on_game_end(info) 每局结束时调用，info 多了 winner、result。
last_info：最近一次 choose 的附加信息（例如大模型的重试次数、是否兜底），对局记录会写进导出数据。
"""
from __future__ import annotations

import random
from typing import Any, Dict, Optional, Sequence

from ..core.game import Game


class Agent:
    """choose(view, decision) -> 选项下标。引擎通过 decide() 调用，智能体拿不到完整状态。"""

    name = "agent"
    needs_view = True      # 不看局面的智能体（如随机）设为 False，省掉生成视角的开销
    needs_history = False  # True：view.logs 里带上该玩家看得到的全部日志文本
    needs_derived = False  # True：view 里带上距离、攻击范围、杀的次数上限等派生量
    last_info: Optional[Dict[str, Any]] = None

    def choose(self, view, d) -> int:  # pragma: no cover
        raise NotImplementedError

    def on_game_start(self, info: Dict[str, Any]) -> None:
        pass

    def on_game_end(self, info: Dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass


def auto_reason(d) -> Optional[str]:
    """不需要想的决策：只有一个合法动作，或者剩下的牌都得选（顺序无关）。返回原因，需要想时返回 None。"""
    if len(d.options) == 1:
        return "唯一选项"
    if (d.ctx or {}).get("forced"):
        return "剩下的牌都要选"
    return None


def decide(agent: Agent, g: Game) -> int:
    """让 agent 在 g 的当前决策点做选择：只把决策者的视角交给它，并检查返回值。"""
    d = g.pending
    view = (g.view(d.player, with_logs=agent.needs_history, derived=agent.needs_derived)
            if agent.needs_view else None)
    i = agent.choose(view, d)
    if not isinstance(i, int) or not 0 <= i < len(d.options):
        raise ValueError(f"{type(agent).__name__} 返回了非法动作 {i!r}（共 {len(d.options)} 个选项）")
    return i


class RandomAgent(Agent):
    name = "random"
    needs_view = False

    def __init__(self, seed: Optional[int] = None, end_bias: float = 0.0):
        self.rng = random.Random(seed)
        self.end_bias = end_bias  # 出牌阶段直接结束的额外概率（0 表示纯随机）

    def choose(self, view, d):
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
