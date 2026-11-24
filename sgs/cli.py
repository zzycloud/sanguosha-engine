"""命令行对战：python -m sgs.cli [--generals 关羽 吕布] [--seed 1]（你是 0 号位，对手是规则 AI）"""
import argparse

from .agents import RuleAgent
from .core.game import Game


def show(g, me):
    o = g.observe(me)
    for p in o["players"]:
        eq = " ".join(c["suit"] + c["rank"] + c["name"] for c in p["equips"].values()) or "无"
        hand = " ".join(c["suit"] + c["rank"] + c["name"] for c in p.get("hand", [])) or f"{p['hand_count']} 张"
        print(f"  [{p['seat']}] {p['general']} {p['hp']}/{p['max_hp']}  手牌：{hand}  装备：{eq}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generals", nargs=2)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    import random
    seed = a.seed if a.seed is not None else random.randrange(10 ** 6)
    kw = {"generals": a.generals} if a.generals else {}
    g = Game("duel", seed=seed, **kw)
    ai = RuleAgent(seed)
    seen = 0
    while not g.over:
        for line in g.visible_logs(0, seen):
            print(line)
        seen = len(g.logs)
        d = g.pending
        if d.player != 0:
            g.act(ai.choose(g, d))
            continue
        show(g, 0)
        print(d.prompt)
        for i, o in enumerate(d.options):
            print(f"  {i}. {o.label}")
        while True:
            s = input("> ").strip()
            if s.isdigit() and int(s) < len(d.options):
                g.act(int(s))
                break
    for line in g.visible_logs(0, seen):
        print(line)


if __name__ == "__main__":
    main()
