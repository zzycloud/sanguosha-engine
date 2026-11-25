"""批量跑对局，按决策点导出 JSONL 训练数据。

    python -m sgs.export --games 1000 --agents rule rule -o data/duel.jsonl
    python -m sgs.export --games 50 --rollouts 8 -o data/labeled.jsonl      # 附 rollout 估值（慢）
    python -m sgs.export --games 10000 --workers 8 -o data/big.jsonl.gz      # .gz 结尾自动压缩

每行是一个决策点，字段说明见 docs/data.md。要点：
- obs / history / legal_actions 都是决策者自己的视角，可以直接喂给策略；
- privileged 是上帝视角的完整状态（含对手手牌和牌堆顺序），只能用于分析或训练 critic，
  不能作为策略输入，否则模型会学会“偷看”；
- outcome 是这局最终结果，reward 已经换算成决策者的角度。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from typing import Any, Dict, Iterator, List, Optional

from .agents.base import decide
from .core.game import Game, log_text
from .core.registry import GENERALS, load_all

FORMAT = "sgs-decision/1"


def make_agent(spec, seed: int):
    """兼容旧接口：spec 可以是 "rule" / "random" / "llm?..." 等任意注册过的智能体写法。"""
    from .agents.registry import make_agent as mk
    return mk(spec, seed=seed)


def _privileged(g: Game) -> Dict[str, Any]:
    """上帝视角：每名角色的完整信息（含手牌）和牌堆顺序（牌 id，第 0 张是牌堆顶）。
    牌 id 对应的牌见 cards 表（导出时写在 <输出>.cards.json，或 card_table(deck)）。"""
    return {
        "hands": {str(p.seat): list(p.hand) for p in g.players},
        "deck_order": list(reversed(g.deck)),
        "discard": list(g.discard),
    }


def card_table(deck: str = "standard") -> List[Dict[str, Any]]:
    """牌 id -> 牌。同一个牌堆名下 id 是固定的（与种子无关）。"""
    g = Game("duel", seed=0, deck=deck, log=False)
    return [g.card_dict(c) for c in range(len(g.cards))]


class DecisionLog:
    """边打边生成导出行：before() 在决策前记下决策者视角，after() 填入选择。一局一个。"""

    def __init__(self, g: Game, game_id: str, agent_names, rollouts: int = 0, history: str = "new",
                 privileged: bool = True):
        self.g = g
        self.game_id = game_id
        self.agent_names = list(agent_names)
        self.rollouts = rollouts
        self.history = history
        self.privileged = privileged
        self.rows: List[Dict[str, Any]] = []
        self.last_log = {s: 0 for s in range(len(g.players))}   # 每个玩家上次决策时看到的日志位置

    def before(self) -> Dict[str, Any]:
        g = self.g
        d = g.pending
        seat = d.player
        view = g.view(seat, derived=True)
        hist_from = 0 if self.history == "full" else self.last_log[seat]
        hist = [t for t in (log_text(e, seat) for e in g.logs[hist_from:]) if t is not None]
        self.last_log[seat] = len(g.logs)
        row: Dict[str, Any] = {
            "format": FORMAT, "game_id": self.game_id, "seed": g.seed, "mode": g.mode_name,
            "step": len(g.decisions), "turn": g.turn_count, "phase": g.phase, "player": seat,
            "general": g.players[seat].general,
            "agent": self.agent_names[seat],
            "obs": view.to_dict(),
            "history": hist, "history_mode": self.history,
            "decision": {"kind": d.kind, "prompt": d.prompt, "ctx": _plain_ctx(d.ctx)},
            "legal_actions": [{"key": _plain(o.key), "label": o.label} for o in d.options],
            "auto": False,
        }
        if self.privileged:
            row["privileged"] = _privileged(g)
        if self.rollouts > 0 and len(d.options) > 1:
            from .rollout import evaluate
            row["rollout_values"] = evaluate(g, self.rollouts, seed=g.seed * 1000 + len(g.decisions))
            row["rollout_n"] = self.rollouts
        row["_view"] = view
        return row

    def after(self, row: Dict[str, Any], i: int, info: Optional[Dict[str, Any]] = None) -> None:
        row.pop("_view", None)
        row["action"] = i
        row["action_key"] = row["legal_actions"][i]["key"]
        row["action_label"] = row["legal_actions"][i]["label"]
        if info:
            row["agent_info"] = info
        self.rows.append(row)

    def finish(self, include_auto: bool = False, result: Optional[str] = None) -> List[Dict[str, Any]]:
        g = self.g
        rows = self.rows
        if include_auto:
            manual_steps = {r["step"] for r in rows}
            for rec in g.decisions:
                if rec["auto"] and rec["i"] not in manual_steps:
                    rows.append({
                        "format": FORMAT, "game_id": self.game_id, "seed": g.seed, "mode": g.mode_name,
                        "step": rec["i"], "turn": rec["turn"], "phase": rec["phase"], "player": rec["player"],
                        "general": g.players[rec["player"]].general, "agent": None, "auto": True,
                        "decision": {"kind": rec["kind"], "prompt": rec["prompt"], "ctx": rec["ctx"]},
                        "legal_actions": rec["options"], "action": 0,
                        "action_key": rec["options"][0]["key"], "action_label": rec["options"][0]["label"],
                    })
            rows.sort(key=lambda r: r["step"])
        outcome = {
            "winner": g.winner, "result": result or g.result, "turns": g.turn_count,
            "generals": [p.general for p in g.players], "num_decisions": len(g.decisions),
            "num_manual": len(g.history),
        }
        for r in rows:
            seat = r["player"]
            reward = 0.0 if g.winner is None else (1.0 if g.winner == seat else -1.0)
            r["outcome"] = dict(outcome, reward=reward)
        return rows


def random_generals(seed: int, n: int = 2) -> List[str]:
    load_all()
    names = sorted(nm for nm, gg in GENERALS.items() if gg.pack == "standard")
    return random.Random(seed).sample(names, n)


def play_and_export(seed: int, agents: List[Any], generals: Optional[List[str]] = None, mode: str = "duel",
                    rollouts: int = 0, include_auto: bool = False, history: str = "new",
                    privileged: bool = True) -> List[Dict[str, Any]]:
    """跑一局，返回这局所有决策点的行（局结束后才填 outcome）。
    agents 里每项可以是智能体写法（"rule"、"llm?model=..."）或 Agent 实例。"""
    from .agents.registry import spec_name
    load_all()
    if generals is None:
        generals = random_generals(seed)
    g = Game(mode, seed=seed, generals=list(generals))
    ags = [make_agent(a, seed * 7 + i) for i, a in enumerate(agents)]
    dl = DecisionLog(g, f"{mode}-{seed}", [spec_name(a) for a in agents], rollouts, history, privileged)
    while not g.over:
        row = dl.before()
        i = decide(ags[row["player"]], g)
        dl.after(row, i, ags[row["player"]].last_info)
        g.act(i)
    return dl.finish(include_auto)


def _plain(x):
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def _plain_ctx(ctx):
    from .core.game import _plain as gp
    return gp(ctx)


def iter_games(games: int, seed: int, **kw) -> Iterator[List[Dict[str, Any]]]:
    for k in range(games):
        yield play_and_export(seed + k, **kw)


def _worker(args):
    s, kw = args
    return play_and_export(s, **kw)


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量对局并导出 JSONL（每行一个决策点）")
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0, help="第一局的种子，之后依次 +1")
    ap.add_argument("--agents", nargs=2, default=["rule", "rule"],
                    help="两个座位的智能体：rule / random / llm?model=...（写法见 docs/agents.md）")
    ap.add_argument("--generals", nargs=2, default=None, help="不填则每局随机两名标准武将")
    ap.add_argument("--rollouts", type=int, default=0, help="每个合法动作做几次 rollout 估值，0 表示不做")
    ap.add_argument("--include-auto", action="store_true", help="也导出只有一个选项、被自动跳过的决策")
    ap.add_argument("--history", choices=["new", "full"], default="new",
                    help="new：只放上次决策以来新出现的日志；full：放从开局起的全部可见日志")
    ap.add_argument("--no-privileged", action="store_true", help="不写上帝视角字段")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("-o", "--out", default="data.jsonl")
    a = ap.parse_args(argv)
    kw = dict(agents=a.agents, generals=a.generals, rollouts=a.rollouts, include_auto=a.include_auto,
              history=a.history, privileged=not a.no_privileged)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    t = time.perf_counter()
    nrows = 0
    seeds = [(a.seed + k, kw) for k in range(a.games)]
    import gzip
    opener = gzip.open if a.out.endswith(".gz") else open
    with opener(a.out, "wt", encoding="utf-8") as f:
        if a.workers > 1:
            from multiprocessing import Pool
            with Pool(a.workers) as pool:
                it = pool.imap(_worker, seeds, chunksize=4)
                for rows in it:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    nrows += len(rows)
        else:
            for s, _ in seeds:
                for r in play_and_export(s, **kw):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    nrows += 1
    base = a.out[:-3] if a.out.endswith(".gz") else a.out
    with open(os.path.splitext(base)[0] + ".cards.json", "w", encoding="utf-8") as f:
        json.dump({"deck": "standard", "cards": card_table()}, f, ensure_ascii=False)
    dt = time.perf_counter() - t
    print(f"{a.games} 局，{nrows} 行 → {a.out}（{dt:.1f} 秒，{a.games / dt:.1f} 局/秒）", file=sys.stderr)


if __name__ == "__main__":
    main()
