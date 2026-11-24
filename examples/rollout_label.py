"""rollout 打标签示例：在一个决策点，对每个合法动作做若干次“确定化 + 模拟到终局”，选胜率最高的。

    python examples/rollout_label.py --seed 3 --rollouts 30

- determinize：把当前玩家看不到的牌（对手手牌 + 牌堆顺序）随机重排，避免偷看隐藏信息；
- 每个动作都在同一批确定化样本上比较（公共随机数），降低方差；
- 模拟策略用规则 AI。换成你的模型即可得到“策略改进”标签。
"""
import argparse
import os
import sys
import random
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sgs import Game  # noqa: E402
from sgs.agents import RuleAgent, play_game  # noqa: E402


def evaluate(g: Game, rollouts: int, seed: int = 0):
    d = g.pending
    me = d.player
    rng = random.Random(seed)
    worlds = [g.determinize(me, random.Random(rng.random())) for _ in range(rollouts)]
    scores = []
    for i, opt in enumerate(d.options):
        win = 0.0
        for k, w in enumerate(worlds):
            c = w.clone()
            c.act(i)
            play_game(c, [RuleAgent(k), RuleAgent(k + 1)])
            win += 1.0 if c.winner == me else (0.5 if c.winner is None else 0.0)
        scores.append(win / rollouts)
    return scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--rollouts", type=int, default=30)
    a = ap.parse_args()
    g = Game(seed=a.seed, generals=["关羽", "吕布"])
    agents = [RuleAgent(1), RuleAgent(2)]
    # 推进到第一个有 3 个以上选项的出牌决策
    while not g.over and not (g.pending.kind == "play" and len(g.pending.options) >= 3):
        g.act(agents[g.pending.player].choose(g, g.pending))
    d = g.pending
    print(f"{g.pname(d.player)}：{d.prompt}")
    t = time.perf_counter()
    scores = evaluate(g, a.rollouts)
    dt = time.perf_counter() - t
    for opt, s in sorted(zip(d.options, scores), key=lambda x: -x[1]):
        print(f"  {s:5.2f}  {opt.label}")
    n = len(d.options) * a.rollouts
    print(f"{n} 次 rollout 用时 {dt:.1f} 秒（{n / dt:.0f} 局/秒）")


if __name__ == "__main__":
    main()
