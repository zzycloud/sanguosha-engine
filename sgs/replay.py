"""对局录像：每个事件（摸牌、判定、弃牌、伤害……每条日志）和每个决策都单独成为一步，带上帝视角状态快照，
导出 JSON 或自带查看器的单文件 HTML。

    python -m sgs.replay --seed 1 --generals 关羽 吕布 --agents rule rule -o replay.html

步的类型（steps[i]["type"]）：
- event：一个引擎事件，logs 里正好一条日志，state 是这个事件发生之后的状态；
- decision：一次询问，decision 是谁、问什么、有哪些选项，chosen 是选了哪个；
  只有一个选项、被自动选掉的也算（auto=true），这样回放里能看到每一次询问和结果；
- end：对局结束（或实时对战里等人决策时的最后一步）。
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
FORMAT = "sgs-replay/3"


def decision_dict(d) -> Dict[str, Any]:
    return {"player": d.player, "kind": d.kind, "prompt": d.prompt, "options": [o.label for o in d.options]}


def skill_category(sk) -> str:
    """技能分类（给人看的标签）：主公技、锁定技、主动技（出牌阶段发动）、转化技、触发技（可选发动）。"""
    from .core.plugin import Skill
    if sk.lord:
        return "主公技"
    if sk.compulsory:
        # 原文写了“锁定技”的标锁定技；咆哮、奇才原文没写，但没有可选的余地，标被动技
        return "锁定技" if "锁定技" in sk.description else "被动技"
    cls = type(sk)
    if cls.can_activate is not Skill.can_activate:
        return "主动技"
    if cls.view_as is not Skill.view_as:
        return "转化技"
    if sk.events or cls.response_options is not Skill.response_options:
        return "触发技"
    return "被动技"


def meta(g: Game) -> Dict[str, Any]:
    """查看器需要的静态信息：技能说明和分类、主公技、牌的类别。"""
    skills = {}
    lord = {}
    names = set()
    for p in g.players:
        names.update(p.skills)
        gen = R.GENERALS.get(p.general)
        if gen and gen.lord_skills:
            lord[p.general] = list(gen.lord_skills)
            names.update(gen.lord_skills)
    for name, spec in R.CARDS.items():
        if spec.skill and spec.skill in R.SKILLS:
            names.add(spec.skill)
    for s in names:
        sk = R.SKILLS[s]
        skills[s] = sk.description
    cats = {s: skill_category(R.SKILLS[s]) for s in names}
    cards = {name: {"type": spec.type, "subtype": spec.subtype} for name, spec in R.CARDS.items()}
    return {"skills": skills, "skill_types": cats, "lord_skills": lord, "cards": cards}


class Recorder:
    """边打边录。agents[seat] 为 None 表示这个座位由外部（人）决定。

    用 Recorder.new_game(...) 创建对局时，开局的每条日志也各自是一步；
    给一个已经开始的 Game 时，之前的日志合成一步“开局”。
    """

    def __init__(self, g: Optional[Game] = None):
        self.steps: List[Dict[str, Any]] = []
        self.g: Optional[Game] = None
        if g is not None:
            self.attach(g)

    @classmethod
    def new_game(cls, *args, **kwargs) -> "Recorder":
        rec = cls()
        g = Game(*args, log_hook=rec._on_log, **kwargs)
        rec.g = g
        return rec

    def attach(self, g: Game) -> None:
        self.g = g
        if g.logs:
            self.steps.append(self._step("event", g, list(g.logs)))
        g.log_hook = self._on_log

    @staticmethod
    def _step(kind, g, logs, decision=None, chosen=None, auto=False) -> Dict[str, Any]:
        return {"type": kind, "state": g.observe(None), "logs": logs, "decision": decision,
                "chosen": chosen, "auto": auto}

    def _on_log(self, g: Game, entry: Dict[str, Any]) -> None:
        self.steps.append(self._step("event", g, [entry]))

    def act(self, i: int) -> None:
        g = self.g
        d = g.pending
        self.steps.append(self._step("decision", g, [], decision_dict(d), i, len(d.options) == 1))
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
        if g.pending is not None and not g.over:
            last = self._step("decision", g, [], decision_dict(g.pending))
        else:
            last = self._step("end", g, [])
        return {
            "format": FORMAT, "seed": g.seed, "mode": g.mode_name,
            "generals": [p.general for p in g.players], "winner": g.winner, "result": g.result,
            "meta": meta(g), "steps": self.steps + [last],
        }


def record(g: Game, agents: Sequence, max_steps: int = 20000) -> Dict[str, Any]:
    """录一个已经创建好的对局（开局日志合成一步）。想让开局每条日志也单独成步，用 record_new。"""
    rec = Recorder(g)
    rec.run(agents, max_steps)
    return rec.export()


def record_new(agents: Sequence, *args, max_steps: int = 20000, **game_kwargs) -> Dict[str, Any]:
    """新建对局并录像：Game(*args, auto_single=False, **game_kwargs)，每个事件单独一步。"""
    game_kwargs.setdefault("auto_single", False)
    rec = Recorder.new_game(*args, **game_kwargs)
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
    rep = record_new([make_agent(a.agents[0], a.seed), make_agent(a.agents[1], a.seed + 1)], "duel", seed=a.seed, **kw)
    with open(a.out, "w", encoding="utf-8") as f:
        if a.out.endswith(".json"):
            json.dump(rep, f, ensure_ascii=False)
        else:
            f.write(to_html(rep))
    print(f"{rep['generals'][0]} vs {rep['generals'][1]}：{rep['result']}，{len(rep['steps'])} 步 → {a.out}")


if __name__ == "__main__":
    main()
