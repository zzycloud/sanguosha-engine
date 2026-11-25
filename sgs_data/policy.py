"""往下模拟用的智能体。

rule+：规则 AI 加一个补丁——观星时如果自己判定区有闪电，把一张不会被劈的牌放在牌堆顶第一张。
原来的规则 AI 观星只挑价值高的牌放上面，完全不管闪电，所以“诸葛亮该不该放闪电”这类题用它模拟会得出错的结论。

只补“规则 AI 明显打错、而且会影响题目答案”的地方，不把你的打法经验写进模拟者，
否则标签就成了“用经验证明经验”。要更强的模拟者，直接在配置里换成你的模型（见 docs/data-gen.md）。
"""
from __future__ import annotations

from sgs.agents.registry import register_agent
from sgs.agents.rule import RuleAgent, card_value


def lightning_hits(card) -> bool:
    return card.suit == "♠" and 2 <= card.rank <= 9


@register_agent("rule+")
class RulePlusAgent(RuleAgent):
    name = "rule+"

    def _choose(self, g, d):
        ctx = d.ctx or {}
        if ctx.get("skill") == "观星" and not ctx.get("top"):
            me = g.players[d.player]
            if any(n == "闪电" for _, n in me.judges):
                safe = [(i, o.key[1]) for i, o in enumerate(d.options)
                        if o.key[0] == "top" and not lightning_hits(g.card(o.key[1]))]
                if safe:
                    return max(safe, key=lambda x: card_value(g, x[1]))[0]
                bottoms = [i for i, o in enumerate(d.options) if o.key[0] == "bottom"]
                if bottoms:
                    return bottoms[0]
        return super()._choose(g, d)
