"""对战：两边各由谁来打（规则 AI、随机 AI、你部署的模型、API），批量并发对局，
输出胜负统计、录像、按决策点的训练数据和模型的原始请求/回复。

所有设置写在一个配置文件里（示例和注释见 configs/），命令行只指定文件、做少量覆盖：

    python -m sgs.arena configs/rule-vs-random.yaml
    python -m sgs.arena configs/local-vllm.yaml --set match.games=50 --set players.A.temperature=0
    python -m sgs.arena configs/local-vllm.yaml --check        # 只检查配置、打印合并后的结果

输出目录里：
  config.yaml       这次实际用的配置（合并了默认值和覆盖，密钥打码），方便复现
  summary.json      胜负统计和每个模型的调用统计（调用次数、不合法输出、兜底次数、耗时、token）
  games.jsonl       每局一行：种子、武将、谁坐哪、结果、用时、出错信息
  decisions.jsonl   每个决策点一行（格式同 sgs.export，见 docs/data.md），多了 agent_info
  llm_calls.jsonl   模型每次决策的完整请求和回复（含重试），格式见 docs/agents.md
  replays/*.html    录像
  cards.json        牌 id 对照表
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

from .agents.base import decide
from .agents.llm.agent import JsonlSink
from . import config as C
from .agents.registry import make_agent, spec_name
from .core.registry import load_all
from .export import DecisionLog, card_table, random_generals
from .replay import Recorder, to_html


class _Writer:
    """线程安全的 JSONL 写入（多局并发写同一个文件）。"""

    def __init__(self, path: Optional[str]):
        self.sink = JsonlSink(path, "w") if path else None

    def __call__(self, rec):
        if self.sink:
            self.sink(rec)

    def close(self):
        if self.sink:
            self.sink.close()


def play_one(k: int, seed: int, specs: Sequence[Any], seats: Sequence[int], generals: Optional[List[str]],
             mode: str = "duel", general_pool: Optional[List[str]] = None, calls: Optional[JsonlSink] = None, decisions: Optional[_Writer] = None,
             replay_path: Optional[str] = None, game_timeout: Optional[float] = None,
             max_steps: int = 20000, rollouts: int = 0, history: str = "new",
             privileged: bool = True) -> Dict[str, Any]:
    """打一局。seats[i] 是第 i 个参赛方（0=A，1=B）坐的座位。返回这局的汇总。"""
    # 武将按座位：[0 号位（先手）, 1 号位]。换边时 A、B 交换座位，牌和武将不变（复式赛制，抵消发牌和武将的运气）
    gens = list(generals) if generals else random_generals(seed, pool=general_pool)
    game_id = f"{mode}-{seed}-{k}"
    t0 = time.perf_counter()
    by_seat: List[Any] = [None, None]
    for side, seat in enumerate(seats):
        by_seat[seat] = specs[side]
    agents = [make_agent(by_seat[s], seed=seed * 7 + s) for s in range(2)]
    for ag in agents:
        if calls is not None and getattr(ag, "sink", "-") is None:
            ag.sink = calls
    rec = Recorder.new_game(mode, seed=seed, generals=gens, auto_single=False)
    g = rec.g
    dl = DecisionLog(g, game_id, [spec_name(by_seat[s]) for s in range(2)], rollouts, history, privileged)
    for s, ag in enumerate(agents):
        ag.on_game_start({"game_id": game_id, "seat": s, "seed": seed, "generals": gens, "mode": mode})
    error = None
    aborted = None
    steps = 0
    try:
        while not g.over:
            if game_timeout and time.perf_counter() - t0 > game_timeout:
                aborted = f"超过 {game_timeout} 秒，中止"
                break
            if steps >= max_steps:
                aborted = f"超过 {max_steps} 步，中止"
                break
            d = g.pending
            if len(d.options) == 1:
                rec.act(0)
            else:
                row = dl.before()
                ag = agents[d.player]
                ag.last_info = None
                i = decide(ag, g)
                dl.after(row, i, ag.last_info)
                rec.act(i)
            steps += 1
    except Exception as e:  # 一局出错不影响其它局
        error = f"{type(e).__name__}: {e}"
        aborted = "出错中止"
        tb = traceback.format_exc()
    else:
        tb = None
    result = g.result if g.over else aborted
    rows = dl.finish(result=result)
    if decisions is not None:
        for r in rows:
            decisions(r)
    if replay_path:
        os.makedirs(os.path.dirname(replay_path), exist_ok=True)
        with open(replay_path, "w", encoding="utf-8") as f:
            f.write(to_html(rec.export()))
    stats = []
    for s, ag in enumerate(agents):
        ag.on_game_end({"game_id": game_id, "seat": s, "winner": g.winner, "result": result})
        stats.append(dict(getattr(ag, "stats", {}) or {}))
        ag.close()
    winner_side = None if g.winner is None else seats.index(g.winner)
    return {"game": k, "game_id": game_id, "seed": seed, "generals": gens, "seats": list(seats),
            "agents": [spec_name(by_seat[s]) for s in range(2)], "winner_seat": g.winner,
            "winner_side": winner_side, "result": result, "turns": g.turn_count, "decisions": len(rows),
            "seconds": round(time.perf_counter() - t0, 3), "agent_stats": stats, "error": error,
            "traceback": tb, "replay": os.path.basename(replay_path) if replay_path else None}


def summarize(games: List[Dict[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"games": len(games), "sides": {}}
    for side, nm in enumerate(names):
        wins = sum(1 for r in games if r["winner_side"] == side)
        first = [r for r in games if r["seats"][side] == 0]
        agg: Dict[str, float] = {}
        for r in games:
            for k2, v in r["agent_stats"][r["seats"][side]].items():
                agg[k2] = agg.get(k2, 0) + v
        side_out: Dict[str, Any] = {"agent": nm, "wins": wins, "win_rate": round(wins / max(1, len(games)), 4),
                                    "games_first": len(first),
                                    "wins_first": sum(1 for r in first if r["winner_side"] == side)}
        if agg:
            dec = max(1, agg.get("decisions", 0))
            side_out["llm"] = {**{k2: (round(v, 3) if isinstance(v, float) else v) for k2, v in agg.items()},
                               "illegal_rate": round(agg.get("illegal", 0) / max(1, agg.get("llm_calls", 0)), 4),
                               "fallback_rate": round(agg.get("fallbacks", 0) / dec, 4),
                               "avg_latency": round(agg.get("latency", 0) / max(1, agg.get("llm_calls", 0)), 3)}
        out["sides"]["AB"[side]] = side_out
    out["draws_or_aborted"] = sum(1 for r in games if r["winner_side"] is None)
    out["errors"] = sum(1 for r in games if r["error"])
    return out


def run_arena(p0: Any, p1: Any, games: int = 10, seed: int = 0, generals: Optional[List[str]] = None,
              swap: bool = False, concurrency: int = 1, out: Optional[str] = None, replays: str = "5",
              game_timeout: Optional[float] = None, max_steps: int = 20000, mode: str = "duel",
              rollouts: int = 0, history: str = "new", privileged: bool = True,
              progress: bool = True, general_pool: Optional[List[str]] = None, write_decisions: bool = True,
              write_llm_calls: bool = True, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Python 里直接调用：run_arena("rule", "llm?model=...&base_url=...", games=10, out="runs/x")。
    一般用 run_config(读好的配置) 或命令行。"""
    load_all()
    specs = [p0, p1]
    names = [spec_name(p0), spec_name(p1)]
    uses_llm = False
    for sp in specs:          # 先建一次，配置写错时立刻报错，而不是每局都失败
        ag = make_agent(sp, seed=0)
        uses_llm = uses_llm or hasattr(ag, "sink")
        ag.close()
    if out:
        os.makedirs(out, exist_ok=True)
    calls = JsonlSink(os.path.join(out, "llm_calls.jsonl"), "w") if (out and write_llm_calls and uses_llm) else None
    decisions = _Writer(os.path.join(out, "decisions.jsonl") if (out and write_decisions) else None)
    if out and config is not None:
        with open(os.path.join(out, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(C.dump_yaml(C.redacted(config)))
    games_w = _Writer(os.path.join(out, "games.jsonl") if out else None)
    replays = str(replays).lower()
    n_rep = games if replays == "all" else (0 if replays in ("none", "0", "false") else int(replays))
    jobs = []
    for k in range(games):
        s = seed + (k // 2 if swap else k)            # 交换先后手时，两局用同一副牌、同一对武将
        seats = (1, 0) if (swap and k % 2 == 1) else (0, 1)
        rp = os.path.join(out, "replays", f"{k:04d}-{mode}-{s}.html") if (out and k < n_rep) else None
        jobs.append(dict(k=k, seed=s, specs=specs, seats=seats, generals=generals, mode=mode,
                         general_pool=general_pool, calls=calls,
                         decisions=decisions, replay_path=rp, game_timeout=game_timeout, max_steps=max_steps,
                         rollouts=rollouts, history=history, privileged=privileged))
    results: List[Dict[str, Any]] = []
    t0 = time.perf_counter()

    def done(r):
        results.append(r)
        games_w({k2: v for k2, v in r.items() if k2 != "traceback"})
        if progress:
            w = "AB"[r["winner_side"]] if r["winner_side"] is not None else "-"
            msg = f"[{len(results)}/{games}] 第 {r['game']} 局 {'/'.join(r['generals'])} 胜方 {w}：{r['result']}"
            if r["error"]:
                msg += f"（{r['error']}）"
            print(msg, file=sys.stderr, flush=True)

    try:
        if concurrency > 1:
            with ThreadPoolExecutor(concurrency) as ex:
                futs = [ex.submit(play_one, **j) for j in jobs]
                for f in as_completed(futs):
                    done(f.result())
        else:
            for j in jobs:
                done(play_one(**j))
    finally:
        for w in (calls, decisions, games_w):
            if w is not None:
                w.close()
    results.sort(key=lambda r: r["game"])
    summary = summarize(results, names)
    summary["seconds"] = round(time.perf_counter() - t0, 2)
    summary["config"] = C.redacted(config) if config is not None else {
        "games": games, "seed": seed, "swap": swap, "concurrency": concurrency, "mode": mode, "generals": generals}
    if out:
        with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out, "cards.json"), "w", encoding="utf-8") as f:
            json.dump({"deck": "standard", "cards": card_table()}, f, ensure_ascii=False)
    summary["results"] = results
    return summary


def run_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按 sgs.config.load() 读好的配置跑一轮对战。"""
    m, o = cfg["match"], cfg["output"]
    out = C.out_dir(cfg)
    return run_arena(
        C.agent_spec(cfg["players"]["A"]), C.agent_spec(cfg["players"]["B"]),
        games=m["games"], seed=m["seed"], generals=C.generals_list(cfg), swap=m["swap"],
        concurrency=m["concurrency"], out=out, replays=o["replays"], game_timeout=m["game_timeout"],
        max_steps=m["max_steps"], mode=m["mode"], rollouts=o["rollouts"], history=o["history"],
        privileged=o["privileged"], progress=o["progress"], general_pool=m["general_pool"],
        write_decisions=o["decisions"], write_llm_calls=o["llm_calls"], config=cfg)


def print_summary(s: Dict[str, Any], out: Optional[str]) -> None:
    for side, v in s["sides"].items():
        line = f"{side} {v['agent']}：{v['wins']}/{s['games']} 胜（先手 {v['wins_first']}/{v['games_first']}）"
        if "llm" in v:
            L = v["llm"]
            line += (f"；模型调用 {L.get('llm_calls', 0)} 次，不合法 {L.get('illegal', 0)}，"
                     f"兜底 {L.get('fallbacks', 0)}，平均 {L['avg_latency']} 秒")
        print(line)
    print(f"平局或中止 {s['draws_or_aborted']}，出错 {s['errors']}，用时 {s['seconds']} 秒" + (f" → {out}" if out else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按配置文件进行对战，输出统计、录像和数据（配置写法见 configs/ 和 docs/agents.md）")
    ap.add_argument("config", nargs="?", default=None, help="配置文件（.yaml/.json/.toml）；不填就用默认值（规则 AI 对打）")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值",
                    help="覆盖配置，可写多次：--set match.games=50 --set players.A.model=qwen")
    ap.add_argument("-o", "--out", default=None, help="输出目录，等于 --set output.dir=...")
    ap.add_argument("--check", action="store_true", help="只检查配置并打印合并后的结果，不开打")
    a = ap.parse_args(argv)
    sets = list(a.set) + ([f"output.dir={a.out}"] if a.out else [])
    try:
        cfg = C.load(a.config, sets)
        if a.check:
            for side in ("A", "B"):
                make_agent(C.agent_spec(cfg["players"][side]), seed=0).close()
            print(C.dump_yaml(C.redacted(cfg)))
            print("配置没有问题。", file=sys.stderr)
            return
    except (C.ConfigError, KeyError, TypeError, OSError) as e:
        print(f"配置有问题：{e}", file=sys.stderr)
        sys.exit(2)
    out = C.out_dir(cfg)
    cfg["output"]["dir"] = out
    s = run_config(cfg)
    print_summary(s, out)


if __name__ == "__main__":
    main()
