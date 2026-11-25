"""打标签：在一个决策点，把每组选项分别往下打到终局（或打几回合后估分），看哪个赢得多。

要点：
- 只用决策者能看到的信息：对手手牌和牌堆每次随机重新分配（Game.determinize，已知手牌留在原处），
  再加上观星补丁：决策者观星后还在牌堆顶/底的牌，留在原来的位置（引擎的 determinize 不管这个）。
- 所有选项在同一批“随机世界”上比较（配对比较），差值的方差小得多，同样的局数能分得更清楚。
- 自适应：先打 n_min 个世界；某个选项和最好的选项差距还说不清时继续加，最多 n_max 个。
- 结论分三种：有明显更差的选项（confident，可以做训练题）；全都差不多（tie，扔掉，学了没用）；
  都说不清（同样当 tie 处理）。
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional, Sequence

from sgs.agents.base import Agent, decide
from sgs.core.game import Game

from .groups import group_options

PolicyFactory = Callable[[], Sequence[Agent]]


# ---------------------------------------------------------------------- 观星补丁
def guanxing_known(g: Game, viewer: int) -> Dict[str, List[int]]:
    """viewer 最近一次观星后，还留在牌堆顶（从上到下）和牌堆底（从下往上）原位的牌。
    要求对局开着日志（g.decisions 记录了观星的每一步）。牌堆洗过就不算知道了。"""
    decs = g.decisions
    last = None
    for i in range(len(decs) - 1, -1, -1):
        dd = decs[i]
        if dd["player"] == viewer and (dd.get("ctx") or {}).get("skill") == "观星":
            last = i
            break
    if last is None:
        return {"top": [], "bottom": []}
    first = last
    while first - 1 >= 0 and decs[first - 1]["player"] == viewer and \
            (decs[first - 1].get("ctx") or {}).get("skill") == "观星" and decs[first - 1]["turn"] == decs[last]["turn"]:
        first -= 1
    top, bottom = [], []
    for dd in decs[first:last + 1]:
        k = dd["options"][dd["chosen"]]["key"]
        if len(k) == 2 and k[0] in ("top", "bottom"):  # 跳过“是否发动观星”这类问题
            (top if k[0] == "top" else bottom).append(k[1])
    # 牌堆顶：top[0] 最先被摸。被摸走的从前面开始少；剩下的必须正好是牌堆最上面几张，否则说明洗过牌
    deck = g.deck
    rest_top = [c for c in top if c in deck]
    if rest_top and list(reversed(deck[-len(rest_top):])) != rest_top:
        rest_top = []
    rest_bottom = [c for c in bottom if c in deck]
    # 牌堆底：bottom 依次插到最底下，所以 deck[0] 是 bottom 的最后一张
    if rest_bottom and list(reversed(deck[:len(rest_bottom)])) != rest_bottom:
        rest_bottom = []
    return {"top": rest_top, "bottom": rest_bottom}


def determinize(g: Game, viewer: int, rng: random.Random, keep_guanxing: bool = True,
                known: Optional[Dict[str, List[int]]] = None) -> Game:
    w = g.determinize(viewer, rng)
    if keep_guanxing and known and (known["top"] or known["bottom"]):
        fixed = known["top"] + known["bottom"]
        fixed_set = set(fixed)
        # 观星看过的牌如果被随机分到了对手手里，和牌堆里一张别的牌换回来
        for p in w.players:
            if p.seat == viewer:
                continue
            for j, c in enumerate(p.hand):
                if c in fixed_set:
                    swap = [x for x in w.deck if x not in fixed_set]
                    if not swap:
                        continue
                    x = rng.choice(swap)
                    w.deck[w.deck.index(x)] = c
                    p.hand[j] = x
        middle = [c for c in w.deck if c not in fixed_set]
        w.deck = list(reversed(known["bottom"])) + middle + list(reversed(known["top"]))
    return w


# ---------------------------------------------------------------------- 估分
def score(g: Game, me: int) -> float:
    if g.winner == me:
        return 1.0
    if g.winner is None:
        return 0.5
    return 0.0


def heuristic(g: Game, me: int) -> float:
    """打到回合上限还没分出胜负时的粗略估分（0~1）：体力占比和手牌、装备数的差。只在 max_turns_ahead 打开时用。"""
    a, b = g.players[me], g.players[1 - me]
    def strength(p):
        return (p.hp / max(1, p.max_hp)) * 3 + 0.15 * len(p.hand) + 0.2 * len(p.equips)
    x = strength(a) - strength(b)
    return 1.0 / (1.0 + math.exp(-x))


def play_out(g: Game, agents: Sequence[Agent], me: int, max_turns_ahead: Optional[int] = None,
             max_steps: int = 20000) -> float:
    stop_turn = g.turn_count + max_turns_ahead if max_turns_ahead else None
    n = 0
    while not g.over and n < max_steps:
        if stop_turn is not None and g.turn_count > stop_turn:
            return heuristic(g, me)
        d = g.pending
        g.act(decide(agents[d.player], g))
        n += 1
    return score(g, me) if g.over else heuristic(g, me)


# ---------------------------------------------------------------------- 主流程
DEFAULT_ROLLOUT = {
    "policy": "rule+",       # 往下模拟时双方用谁打
    "n_min": 32,             # 每组选项至少模拟几个随机世界
    "n_max": 128,            # 最多几个
    "batch": 16,             # 每次加几个
    "margin": 0.06,          # 胜率差小于它算“差不多”
    "z": 2.0,                # 显著性：差值要超过 z 倍标准误
    "keep_guanxing": True,
    "max_turns_ahead": None, # 只往下打几回合（null 打到终局）
}


def evaluate(g: Game, policy: PolicyFactory, cfg: Dict[str, Any], seed: int = 0) -> Dict[str, Any]:
    """返回 groups / group_labels / values / n / diffs / worse / acceptable / best / status。"""
    c = dict(DEFAULT_ROLLOUT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT_ROLLOUT})
    d = g.pending
    me = d.player
    groups, labels = group_options(g, d)
    G = len(groups)
    out: Dict[str, Any] = {"groups": groups, "group_labels": labels}
    if G < 2:
        out.update(values=[1.0], n=0, status="single", best=0, acceptable=[0], worse=[], diffs=[0.0], se=[0.0])
        return out
    rng = random.Random(seed)
    known = guanxing_known(g, me) if c["keep_guanxing"] else None
    agents = policy()
    want_logs = any(getattr(a, "needs_history", False) for a in agents)   # 模型当模拟者时要看近况
    results: List[List[float]] = [[] for _ in range(G)]
    n = 0
    n_min, n_max = int(c["n_min"]), max(int(c["n_max"]), int(c["n_min"]))
    margin, z = float(c["margin"]), float(c["z"])

    def stats():
        means = [sum(r) / len(r) for r in results]
        b = max(range(G), key=lambda j: means[j])
        diffs, ses = [], []
        for j in range(G):
            dk = [results[b][k] - results[j][k] for k in range(n)]
            m = sum(dk) / n
            var = sum((x - m) ** 2 for x in dk) / (n - 1) if n > 1 else 0.0
            diffs.append(m)
            # 下限：世界太少时差值碰巧全一样，方差算出来是 0，会“过度自信”；按胜率 50% 附近的方差给个保底
            ses.append(0.0 if j == b else max(math.sqrt(var / n), 0.25 / math.sqrt(n)))
        return means, b, diffs, ses

    while n < n_max:
        step = min(int(c["batch"]), n_max - n)
        for _ in range(step):
            w = determinize(g, me, random.Random(rng.random()), c["keep_guanxing"], known)
            w.log_hook = None
            if not want_logs:
                w.logging = False
                w.logs, w.decisions = [], []
            for j, grp in enumerate(groups):
                x = w.clone()
                x.act(grp[0])
                results[j].append(play_out(x, agents, me, c["max_turns_ahead"]))
            n += 1
        if n < n_min:
            continue
        means, b, diffs, ses = stats()
        settled = all(j == b or (diffs[j] >= margin and diffs[j] - z * ses[j] > 0) or (diffs[j] + z * ses[j] < margin)
                      for j in range(G))
        if settled:
            break
    means, b, diffs, ses = stats()
    worse = [j for j in range(G) if j != b and diffs[j] >= margin and diffs[j] - z * ses[j] > 0]
    acceptable = [j for j in range(G) if j not in worse]
    out.update(values=[round(m, 4) for m in means], n=n, best=b, diffs=[round(x, 4) for x in diffs],
               se=[round(x, 4) for x in ses], worse=worse, acceptable=acceptable,
               status="confident" if worse else "tie")
    for a in agents:
        try:
            a.close()
        except Exception:
            pass
    return out


def make_policy(spec: Any, base_dir: str = ".") -> PolicyFactory:
    """配置里的 policy：rule / rule+ / random / {include: 模型配置.yaml, ...} / {agent: llm, ...}。双方用同一种。"""
    from sgs import config as C
    from sgs.agents.registry import make_agent
    from . import policy as _p  # noqa: F401  注册 rule+

    if isinstance(spec, dict):
        p = C.resolve_player("rollout.policy", spec, base_dir)
        agent_spec = C.agent_spec(p)
    else:
        agent_spec = spec
    counter = [0]

    def factory() -> List[Agent]:
        counter[0] += 1
        k = counter[0] * 2
        return [make_agent(agent_spec, seed=k), make_agent(agent_spec, seed=k + 1)]
    return factory


def policy_is_llm(spec: Any) -> bool:
    if isinstance(spec, dict):
        return spec.get("agent", "llm") == "llm" or "include" in spec
    return str(spec).startswith("llm")
