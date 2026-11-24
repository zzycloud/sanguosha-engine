"""对局录像：记录每个决策点的上帝视角状态、可选项和实际选择，导出 JSON 或单文件 HTML。

    python -m sgs.replay --seed 1 --generals 关羽 吕布 --agents rule rule -o replay.html
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional, Sequence

from .core.game import Game

VIEWER = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")


def decision_dict(d) -> Dict:
    return {"player": d.player, "kind": d.kind, "prompt": d.prompt, "options": [o.label for o in d.options]}


def record(g: Game, agents: Sequence, max_steps: int = 5000) -> Dict:
    steps: List[Dict] = []
    seen = 0
    n = 0
    while not g.over and n < max_steps:
        d = g.pending
        i = agents[d.player].choose(g, d)
        steps.append({"state": g.observe(None), "logs": g.logs[seen:], "decision": decision_dict(d), "chosen": i})
        seen = len(g.logs)
        g.act(i)
        n += 1
    steps.append({"state": g.observe(None), "logs": g.logs[seen:], "decision": None, "chosen": None})
    return {
        "format": "sgs-replay/1", "seed": g.seed, "mode": g.mode_name,
        "generals": [p.general for p in g.players], "winner": g.winner, "result": g.result,
        "steps": steps,
    }


def to_html(rep: Dict, viewer_path: Optional[str] = None) -> str:
    with open(viewer_path or VIEWER, encoding="utf-8") as f:
        html = f.read()
    data = json.dumps(rep, ensure_ascii=False).replace("</", "<\\/")
    return html.replace("<script id=\"embedded-replay\" type=\"application/json\"></script>",
                        f"<script id=\"embedded-replay\" type=\"application/json\">{data}</script>")


def make_agent(kind: str, seed: int):
    from .agents import RandomAgent, RuleAgent
    return {"random": RandomAgent, "rule": RuleAgent}[kind](seed)


def main():
    ap = argparse.ArgumentParser(description="录制一局对局")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--generals", nargs=2, default=None)
    ap.add_argument("--agents", nargs=2, default=["rule", "rule"], choices=["rule", "random"])
    ap.add_argument("-o", "--out", default="replay.html", help=".html（自带查看器）或 .json")
    a = ap.parse_args()
    kw = {"generals": a.generals} if a.generals else {}
    g = Game("duel", seed=a.seed, **kw)
    rep = record(g, [make_agent(a.agents[0], a.seed), make_agent(a.agents[1], a.seed + 1)])
    with open(a.out, "w", encoding="utf-8") as f:
        if a.out.endswith(".json"):
            json.dump(rep, f, ensure_ascii=False)
        else:
            f.write(to_html(rep))
    print(f"{rep['generals'][0]} vs {rep['generals'][1]}：{rep['result']}，{len(rep['steps'])} 步 → {a.out}")


if __name__ == "__main__":
    main()
