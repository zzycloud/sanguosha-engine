"""测速与基线：python -m sgs.tools.bench [--games 2000]"""
import argparse
import random
import time

from ..agents import RandomAgent, RuleAgent, play_game
from ..core.game import Game
from ..core.registry import GENERALS, load_all


def run(n, make_agents, log=False, seed0=0):
    load_all()
    names = list(GENERALS)
    wins = [0, 0, 0]
    decisions = 0
    t = time.perf_counter()
    for i in range(n):
        rng = random.Random(seed0 + i)
        gens = rng.sample(names, 2)
        g = Game(seed=seed0 + i, generals=gens, log=log)
        play_game(g, make_agents(i))
        decisions += len(g.history)
        wins[2 if g.winner is None else g.winner] += 1
    dt = time.perf_counter() - t
    return n / dt, decisions / dt, wins


def bench_clone(n=2000):
    g = Game(seed=1, log=False)
    ag = [RandomAgent(1), RandomAgent(2)]
    for _ in range(40):
        if g.over:
            break
        g.act(ag[g.pending.player].choose(g, g.pending))
    t = time.perf_counter()
    for _ in range(n):
        g.clone()
    clone_us = (time.perf_counter() - t) / n * 1e6
    t = time.perf_counter()
    k = 0
    while time.perf_counter() - t < 2.0:
        c = g.clone()
        play_game(c, [RandomAgent(k), RandomAgent(k + 1)])
        k += 1
    return clone_us, k / (time.perf_counter() - t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=2000)
    a = ap.parse_args()
    gps, dps, w = run(a.games, lambda i: [RandomAgent(i), RandomAgent(i + 1)])
    print(f"随机 vs 随机（关闭日志）：{gps:.0f} 局/秒，{dps:.0f} 决策/秒，胜负 {w}")
    gps, dps, w = run(a.games // 4, lambda i: [RandomAgent(i), RandomAgent(i + 1)], log=True)
    print(f"随机 vs 随机（开启日志）：{gps:.0f} 局/秒")
    gps, dps, w = run(a.games // 2, lambda i: [RuleAgent(i), RandomAgent(i)])
    print(f"规则(0号位) vs 随机：{gps:.0f} 局/秒，0号位胜 {w[0]}，1号位胜 {w[1]}，平 {w[2]}")
    gps, dps, w = run(a.games // 2, lambda i: [RandomAgent(i), RuleAgent(i)])
    print(f"随机(0号位) vs 规则：0号位胜 {w[0]}，1号位胜 {w[1]}，平 {w[2]}")
    gps, dps, w = run(a.games // 2, lambda i: [RuleAgent(i), RuleAgent(i + 1)])
    print(f"规则 vs 规则：{gps:.0f} 局/秒，0号位胜 {w[0]}，1号位胜 {w[1]}，平 {w[2]}")
    us, rps = bench_clone()
    print(f"中盘状态 clone：{us:.0f} 微秒/次；从中盘 clone 后随机打完：{rps:.0f} 局/秒")


if __name__ == "__main__":
    main()
