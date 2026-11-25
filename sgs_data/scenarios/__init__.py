"""关键局面模板。每个模板摆出一类“值得想”的决策点，并且故意混进反例（答案应该反过来的局面）。

模板只决定“在哪里出题、局面怎么随机变化”，不决定答案：答案由往下模拟得出。
新增模板：写一个 Scenario 子类，@register_scenario，配置里写它的名字就能用。
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from sgs.core.game import Game

from ..board import BuildFailed

SCENARIOS: Dict[str, type] = {}


def register_scenario(cls):
    SCENARIOS[cls.name] = cls
    return cls


class Scenario:
    name = ""
    general = ""         # 出题的武将
    title = ""           # 中文名
    question = ""        # 这类题在问什么
    counter = ""         # 反例是什么
    hero_seat = 0

    def __init__(self, **params: Any):
        self.params = params

    def build(self, seed: int) -> Tuple[Game, Dict[str, Any]]:  # pragma: no cover
        """摆出局面，返回 (停在出题决策点的对局, 这道题的变化参数)。摆不出来抛 BuildFailed。"""
        raise NotImplementedError

    def build_retry(self, seed: int, tries: int = 20) -> Tuple[Game, Dict[str, Any], int]:
        """摆不出来就换种子（种子 = seed*100 + 第几次），返回实际用的种子，题目配方里存它。"""
        last = None
        for t in range(tries):
            s = seed * 100 + t
            try:
                g, var = self.build(s)
            except BuildFailed as e:
                last = e
                continue
            from ..groups import group_options
            if len(group_options(g, g.pending)[0]) < 2:      # 只剩一种选择，不用想
                last = BuildFailed("只有一种选择")
                continue
            return g, var, s
        raise BuildFailed(f"{self.name} 种子 {seed} 试了 {tries} 次都摆不出来：{last}")

    def tag(self, desc: str) -> str:
        """答案标签（配比时按它平衡正例反例）。默认就是最佳选项的描述。"""
        return desc

    def pick(self, rng: random.Random, table: List[Tuple[Any, float]]) -> Any:
        total = sum(w for _, w in table)
        x = rng.random() * total
        for v, w in table:
            x -= w
            if x <= 0:
                return v
        return table[-1][0]

    def opponents(self, exclude: Tuple[str, ...] = ()) -> List[str]:
        from ..board import standard_generals
        pool = self.params.get("opponents") or standard_generals()
        return [n for n in pool if n != self.general and n not in exclude]


def make_scenario(name: str, params: Optional[Dict[str, Any]] = None) -> Scenario:
    _load()
    if name not in SCENARIOS:
        raise KeyError(f"没有这个关键局面模板：{name}；有：{', '.join(sorted(SCENARIOS))}")
    return SCENARIOS[name](**(params or {}))


def _load():
    from . import guojia, simayi, zhenji, zhuge  # noqa: F401


def all_scenarios() -> Dict[str, type]:
    _load()
    return dict(SCENARIOS)
