"""强模型当裁判：同一道题问它 votes 次（带理由），看它选的和模拟结论是否一致。

用处：
- 模拟（尤其是规则 AI 模拟）有系统性偏差，比如它自己永远不会故意不出闪。裁判和模拟一致的题更可信；
  不一致的题不进训练，放进 review.md 给人看（on_disagree: review）。
- 裁判的理由保存在题目里，以后要做“带理由”的数据可以直接用。

它只看决策者视角的提示词（和你推理时同一套模板，只是要求先写理由），看不到对手手牌。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from sgs.agents.llm.parse import parse_action
from sgs.agents.llm.client import ChatError

from .groups import group_of

JUDGE_RULES = ("这一步可能是“短期吃亏、长期占优”的关键选择：先想清楚每个选项接下来两三个回合会发生什么，"
               "再选最终最可能赢的那个。")


def make_judge_agent(spec: Dict[str, Any], base_dir: str):
    from sgs import config as C
    from sgs.agents.registry import make_agent
    p = C.resolve_player("judge.player", spec, base_dir)
    p.setdefault("ask_reason", True)
    p.setdefault("extra_rules", JUDGE_RULES)
    p.setdefault("fallback", "none")
    return make_agent(C.agent_spec(p))


def judge_item(agent, g, item: Dict[str, Any], votes: int) -> Dict[str, Any]:
    d = g.pending
    view = g.view(d.player, with_logs=True, derived=True)
    messages = agent.prompt.messages(view, d)
    labels = [o.label for o in d.options]
    groups = item["groups"]
    picks: List[int] = []
    reasons: List[Dict[str, Any]] = []
    errors = 0
    for _ in range(votes):
        try:
            r = agent.client.chat(messages, **agent.params)
        except ChatError:
            errors += 1
            continue
        i, err, why = parse_action(r.text, len(labels), labels)
        if err is not None:
            errors += 1
            continue
        gi = group_of(groups, i)
        picks.append(gi)
        reasons.append({"group": gi, "reason": why})
    cnt = Counter(picks)
    if not picks:
        return {"votes": 0, "errors": errors, "agree": None}
    top, top_n = cnt.most_common(1)[0]
    ok = set(item.get("acceptable") or [])
    in_ok = sum(v for k, v in cnt.items() if k in ok)
    return {"votes": len(picks), "errors": errors, "counts": {str(k): v for k, v in sorted(cnt.items())},
            "majority": top, "majority_share": round(top_n / len(picks), 3),
            "agree_share": round(in_ok / len(picks), 3), "agree": in_ok / len(picks) >= 0.5,
            "reasons": reasons[:votes]}
