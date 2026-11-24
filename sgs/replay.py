"""对局录像：记录每个决策点（包括只有一个选项的决策）的上帝视角状态、新增日志、可选项和实际选择，
导出 JSON 或自带查看器的单文件 HTML。

    python -m sgs.replay --seed 1 --generals 关羽 吕布 --agents rule rule -o replay.html

录像用 auto_single=False 跑：只有一个选项的决策（比如手里没有闪时的“不出”）也会单独成为一步，
标记 auto=true，这样回放里能看到每一次询问和结果。
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from .agents.base import decide
from .core import registry as R
from .core.game import Game

VIEWER = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")
FORMAT = "sgs-replay/2"


def decision_dict(d) -> Dict[str, Any]:
    return {"player": d.player, "kind": d.kind, "prompt": d.prompt, "options": [o.label for o in d.options]}


def meta(g: Game) -> Dict[str, Any]:
    """查看器需要的静态信息：技能说明、牌的类别。"""
    skills = {}
    for p in g.players:
        for s in p.skills:
            skills[s] = R.SKILLS[s].description
    for name, spec in R.CARDS.items():
        if spec.skill and spec.skill in R.SKILLS:
            skills[spec.skill] = R.SKILLS[spec.skill].description
    cards = {name: {"type": spec.type, "subtype": spec.subtype} for name, spec in R.CARDS.items()}
    return {"skills": skills, "cards": cards}


class Recorder:
    """边打边录。agents[seat] 为 None 表示这个座位由外部（人）决定。"""

    def __init__(self, g: Game):
        self.g = g
        self.steps: List[Dict[str, Any]] = []
        self.seen = 0

    def act(self, i: int) -> None:
        g = self.g
        d = g.pending
        self.steps.append({"state": g.observe(None), "logs": g.logs[self.seen:], "decision": decision_dict(d),
                           "chosen": i, "auto": len(d.options) == 1})
        self.seen = len(g.logs)
        g.act(i)

    def run(self, agents: Sequence, max_steps: int = 20000) -> None:
        """自动推进：单选项决策直接选，其它交给 agents；轮到 agents 里为 None 的座位时停下。"""
        g = self.g
        n = 0
        while not g.over and n < max_steps:
            d = g.pending
            if len(d.options) == 1:
                self.act(0)
            elif agents[d.player] is None:
                return
            else:
                self.act(decide(agents[d.player], g))
            n += 1

    def export(self) -> Dict[str, Any]:
        g = self.g
        last = {"state": g.observe(None), "logs": g.logs[self.seen:],
                "decision": decision_dict(g.pending) if g.pending else None, "chosen": None, "auto": False}
        return {
            "format": FORMAT, "seed": g.seed, "mode": g.mode_name,
            "generals": [p.general for p in g.players], "winner": g.winner, "result": g.result,
            "meta": meta(g), "steps": self.steps + [last],
        }


def record(g: Game, agents: Sequence, max_steps: int = 20000) -> Dict[str, Any]:
    rec = Recorder(g)
    rec.run(agents, max_steps)
    return rec.export()


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
    kw = {"generals": a.generals} if a.generals else {"pick": "random"}
    g = Game("duel", seed=a.seed, auto_single=False, **kw)
    rep = record(g, [make_agent(a.agents[0], a.seed), make_agent(a.agents[1], a.seed + 1)])
    with open(a.out, "w", encoding="utf-8") as f:
        if a.out.endswith(".json"):
            json.dump(rep, f, ensure_ascii=False)
        else:
            f.write(to_html(rep))
    print(f"{rep['generals'][0]} vs {rep['generals'][1]}：{rep['result']}，{len(rep['steps'])} 步 → {a.out}")


if __name__ == "__main__":
    main()
