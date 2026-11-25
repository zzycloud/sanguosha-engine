"""对战：两边各由谁来打（规则 AI、随机 AI、你部署的模型、API），批量并发对局，
每局的录像、轨迹和模型调用放在同一个目录里，按步数对齐；顶层有总索引页和汇总统计。

所有设置写在一个配置文件里（示例和注释见 configs/），命令行只指定文件、做少量覆盖：

    python -m sgs.arena configs/rule-vs-random.yaml
    python -m sgs.arena configs/local-vllm.yaml --set match.games=50 --set players.A.temperature=0
    python -m sgs.arena configs/local-vllm.yaml --check        # 检查配置，并对模型做一次连通性自检

输出目录：
  index.html          总索引：每局胜负、武将、模型调用/答错/兜底次数，点进去看录像和轨迹
  summary.json        胜负统计和每个模型的调用统计（调用次数、不合法输出、兜底、按类型的请求错误、耗时、token）
  games.json          每局一条汇总
  config.yaml         这次实际用的配置（合并了默认值和覆盖，密钥打码），方便复现
  cards.json          牌 id 对照表
  games/0001/         每局一个目录：replay.html、game.md、trajectory.json、llm_calls.json、result.json
                      （各文件说明见 sgs/gamedir.py 和 docs/agents.md）
  decisions.jsonl     （可选，output.decisions）所有局的决策点合并成一个 JSONL，训练用，格式见 docs/data.md
  llm_calls.jsonl     （可选，output.llm_calls）所有局的模型调用合并成一个 JSONL
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

from .agents.base import decide
from .agents.llm.agent import JsonlSink
from . import config as C
from . import gamedir as GD
from .agents.registry import make_agent, spec_name
from .core.registry import load_all
from .export import DecisionLog, card_table, random_generals
from .replay import Recorder, to_html


class _Writer:
    """线程安全的 JSONL 写入（多局并发写同一个文件）；一局的行一次写完，不和别的局交错。"""

    def __init__(self, path: Optional[str]):
        self.sink = JsonlSink(path, "w") if path else None
        self._lock = threading.Lock()

    def __call__(self, rec):
        if self.sink:
            self.sink(rec)

    def many(self, recs):
        if self.sink:
            with self._lock:
                for r in recs:
                    self.sink(r)

    def close(self):
        if self.sink:
            self.sink.close()


def _sum_stats(stats: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for st in stats:
        for k, v in (st or {}).items():
            if isinstance(v, dict):
                d = out.setdefault(k, {})
                for k2, v2 in v.items():
                    d[k2] = d.get(k2, 0) + v2
            elif isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
    return out


def play_one(k: int, seed: int, specs: Sequence[Any], seats: Sequence[int], generals: Optional[List[str]],
             mode: str = "duel", general_pool: Optional[List[str]] = None, calls: Optional[_Writer] = None,
             decisions: Optional[_Writer] = None, game_dir: Optional[str] = None, replay: bool = True,
             game_timeout: Optional[float] = None, max_steps: int = 20000, rollouts: int = 0,
             history: str = "new", privileged: bool = True, concurrency: int = 1,
             replay_path: Optional[str] = None) -> Dict[str, Any]:
    """打一局。k 是第几局（从 1 开始），seats[i] 是第 i 个参赛方（0=A，1=B）坐的座位。返回这局的汇总。

    game_dir 不为空时把这局的录像、轨迹、模型调用写进这个目录（见 sgs/gamedir.py）。"""
    # 武将按座位：[0 号位（先手）, 1 号位]。换边时 A、B 交换座位，牌和武将不变（复式赛制，抵消发牌和武将的运气）
    gens = list(generals) if generals else random_generals(seed, pool=general_pool)
    game_id = f"{mode}-{seed}-{k}"
    t0 = time.perf_counter()
    by_seat: List[Any] = [None, None]
    for side, seat in enumerate(seats):
        by_seat[seat] = specs[side]
    names = [spec_name(by_seat[s]) for s in range(2)]
    agents = [make_agent(by_seat[s], seed=seed * 7 + s) for s in range(2)]
    game_calls: List[Dict[str, Any]] = []       # 这局的模型调用，按时间顺序
    for ag in agents:
        if hasattr(ag, "sink"):
            orig = ag.sink
            ag.sink = (lambda r, o=orig: (game_calls.append(r), o(r) if o is not None else None))
    rec = Recorder.new_game(mode, seed=seed, generals=gens, auto_single=False)
    g = rec.g
    dl = DecisionLog(g, game_id, names, rollouts, history, privileged)
    for s, ag in enumerate(agents):
        ag.on_game_start({"game_id": game_id, "game": k, "seat": s, "seed": seed, "generals": gens, "mode": mode,
                          "concurrency": concurrency})
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
                rs = len(rec.steps)                 # 这个决策在录像里是第几步
                row["replay_step"] = rs
                row["log_index"] = len(g.logs)      # 决策前一共发生了多少条日志（对齐用）
                ag = agents[d.player]
                ag.last_info = None
                n0 = len(game_calls)
                i = decide(ag, g)
                new = game_calls[n0:]
                for j, c in enumerate(new, n0):
                    c["call"] = j
                    c["replay_step"] = rs
                    c["decision_step"] = row["step"]
                row["llm_calls"] = [c["call"] for c in new]
                dl.after(row, i, ag.last_info)
                rec.act(i)
                st = rec.steps[rs]
                st["agent"] = names[d.player]
                if ag.last_info:
                    st["info"] = ag.last_info
                if new:
                    st["calls"] = row["llm_calls"]
            steps += 1
    except Exception as e:  # 一局出错不影响其它局
        error = f"{type(e).__name__}: {e}"
        aborted = "出错中止"
        tb = traceback.format_exc()
    else:
        tb = None
    result = g.result if g.over else aborted
    rows = dl.finish(result=result)
    stats = []
    for s, ag in enumerate(agents):
        ag.on_game_end({"game_id": game_id, "seat": s, "winner": g.winner, "result": result})
        stats.append(dict(getattr(ag, "stats", {}) or {}))
        ag.close()
    tot = _sum_stats(stats)
    winner_side = None if g.winner is None else seats.index(g.winner)
    dname = f"{GD.GAMES_DIR}/{GD.game_dirname(k)}" if game_dir else None
    info = {"game": k, "game_id": game_id, "seed": seed, "generals": gens, "seats": list(seats),
            "agents": names, "winner_seat": g.winner, "winner_side": winner_side, "result": result,
            "turns": g.turn_count, "decisions": len(rows), "llm_calls": len(game_calls), "llm_requests": tot.get("llm_calls", 0),
            "illegal": tot.get("illegal", 0), "fallbacks": tot.get("fallbacks", 0),
            "request_errors": tot.get("errors", 0), "error_kinds": tot.get("error_kinds", {}),
            "seconds": round(time.perf_counter() - t0, 3), "agent_stats": stats, "error": error,
            "traceback": tb, "dir": dname, "replay": bool(game_dir and replay) or bool(replay_path)}
    if decisions is not None:
        decisions.many(rows)
    if calls is not None:
        calls.many(game_calls)
    rep = None
    if replay and (game_dir or replay_path):
        rep = rec.export()
        rep["agents"] = names
        rep["llm_calls"] = game_calls
        rep["game"] = k
    if replay_path:
        os.makedirs(os.path.dirname(os.path.abspath(replay_path)), exist_ok=True)
        with open(replay_path, "w", encoding="utf-8") as f:
            f.write(to_html(rep))
    if game_dir:
        os.makedirs(game_dir, exist_ok=True)
        if rep is not None:
            with open(os.path.join(game_dir, "replay.html"), "w", encoding="utf-8") as f:
                f.write(to_html(rep))
        GD.write_json(os.path.join(game_dir, "trajectory.json"), {
            "format": "sgs-trajectory/1", "game": k, "game_id": game_id, "seed": seed, "mode": mode,
            "generals": gens, "agents": names, "winner_seat": g.winner, "result": result,
            "replay": "replay.html" if rep is not None else None,
            "note": "decisions 里每项是一个需要选择的决策点（决策者视角）；replay_step 对应录像第几步，"
                    "llm_calls 是 llm_calls.json 里的下标",
            "decisions": rows})
        GD.write_json(os.path.join(game_dir, "llm_calls.json"), game_calls)
        GD.write_json(os.path.join(game_dir, "result.json"), info)
        with open(os.path.join(game_dir, "game.md"), "w", encoding="utf-8") as f:
            f.write(GD.game_markdown(info, g.logs, rows, game_calls))
    return info


def summarize(games: List[Dict[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"games": len(games), "sides": {}}
    for side, nm in enumerate(names):
        wins = sum(1 for r in games if r["winner_side"] == side)
        first = [r for r in games if r["seats"][side] == 0]
        agg = _sum_stats([r["agent_stats"][r["seats"][side]] for r in games])
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
              swap: bool = False, concurrency: int = 1, out: Optional[str] = None, replays: Any = "all",
              game_timeout: Optional[float] = None, max_steps: int = 20000, mode: str = "duel",
              rollouts: int = 0, history: str = "new", privileged: bool = True,
              progress: bool = True, general_pool: Optional[List[str]] = None, write_decisions: bool = False,
              write_llm_calls: bool = False, per_game: bool = True,
              config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    calls = _Writer(os.path.join(out, "llm_calls.jsonl")) if (out and write_llm_calls and uses_llm) else None
    decisions = _Writer(os.path.join(out, "decisions.jsonl")) if (out and write_decisions) else None
    if out and config is not None:
        with open(os.path.join(out, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(C.dump_yaml(C.redacted(config)))
    if out:
        with open(os.path.join(out, "cards.json"), "w", encoding="utf-8") as f:
            json.dump({"deck": "standard", "cards": card_table()}, f, ensure_ascii=False)
    rp = str(replays).lower()
    n_rep = games if rp == "all" else (0 if rp in ("none", "0", "false") else int(rp))
    jobs = []
    for k in range(1, games + 1):
        s = seed + ((k - 1) // 2 if swap else k - 1)        # 交换先后手时，两局用同一副牌、同一对武将
        seats = (1, 0) if (swap and k % 2 == 0) else (0, 1)
        gd = os.path.join(out, GD.GAMES_DIR, GD.game_dirname(k)) if (out and per_game) else None
        jobs.append(dict(k=k, seed=s, specs=specs, seats=seats, generals=generals, mode=mode,
                         general_pool=general_pool, calls=calls, decisions=decisions, game_dir=gd,
                         replay=k <= n_rep, game_timeout=game_timeout, max_steps=max_steps,
                         rollouts=rollouts, history=history, privileged=privileged, concurrency=concurrency))
    results: List[Dict[str, Any]] = []
    t0 = time.perf_counter()

    def write_index():
        if not out:
            return
        rs = sorted(results, key=lambda r: r["game"])
        sm = summarize(rs, names)
        sm["seconds"] = round(time.perf_counter() - t0, 2)
        GD.write_json(os.path.join(out, "games.json"), [{k2: v for k2, v in r.items() if k2 != "traceback"} for r in rs])
        with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
            f.write(GD.index_html(sm, rs, title=os.path.basename(os.path.normpath(out))))

    def done(r):
        results.append(r)
        write_index()               # 每打完一局就更新索引页，跑的过程中也能看
        if progress:
            w = "AB"[r["winner_side"]] if r["winner_side"] is not None else "-"
            msg = f"[{len(results)}/{games}] 第 {r['game']} 局 {'/'.join(r['generals'])} 胜方 {w}：{r['result']}"
            if r["llm_calls"]:
                msg += f"；模型决策 {r['llm_calls']}（请求 {r['llm_requests']} 次），答错 {r['illegal']}，兜底 {r['fallbacks']}"
            if r["error_kinds"]:
                msg += "，请求失败 " + "、".join(f"{k2}×{v}" for k2, v in r["error_kinds"].items())
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
        for w in (calls, decisions):
            if w is not None:
                w.close()
    results.sort(key=lambda r: r["game"])
    summary = summarize(results, names)
    summary["seconds"] = round(time.perf_counter() - t0, 2)
    summary["config"] = C.redacted(config) if config is not None else {
        "games": games, "seed": seed, "swap": swap, "concurrency": concurrency, "mode": mode, "generals": generals}
    if out:
        write_index()
        GD.write_json(os.path.join(out, "summary.json"), summary)
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
        write_decisions=o["decisions"], write_llm_calls=o["llm_calls"], per_game=o["per_game"], config=cfg)


def print_summary(s: Dict[str, Any], out: Optional[str]) -> None:
    for side, v in s["sides"].items():
        line = f"{side} {v['agent']}：{v['wins']}/{s['games']} 胜（先手 {v['wins_first']}/{v['games_first']}）"
        if "llm" in v:
            L = v["llm"]
            line += (f"；模型决策 {L.get('decisions', 0)} 次（请求 {L.get('llm_calls', 0)} 次），答错 {L.get('illegal', 0)}，"
                     f"兜底 {L.get('fallbacks', 0)}，平均 {L['avg_latency']} 秒")
            if L.get("error_kinds"):
                line += "；请求失败 " + "、".join(f"{k}×{v}" for k, v in L["error_kinds"].items())
        print(line)
    print(f"平局或中止 {s['draws_or_aborted']}，出错 {s['errors']}，用时 {s['seconds']} 秒" + (f" → {out}" if out else ""))
    if out:
        print(f"打开 {os.path.join(out, 'index.html')} 看每局的录像、轨迹和模型调用")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按配置文件进行对战，输出统计、录像和数据（配置写法见 configs/ 和 docs/agents.md）")
    ap.add_argument("config", nargs="?", default=None, help="配置文件（.yaml/.json/.toml）；不填就用默认值（规则 AI 对打）")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值",
                    help="覆盖配置，可写多次：--set match.games=50 --set players.A.model=qwen")
    ap.add_argument("-o", "--out", default=None, help="输出目录，等于 --set output.dir=...")
    ap.add_argument("--check", action="store_true",
                    help="只检查配置并打印合并后的结果，再对模型做一次连通性自检（打印地址、是否走代理、错误细节），不开打")
    ap.add_argument("--no-connect", action="store_true", help="和 --check 一起用：不做连通性自检")
    a = ap.parse_args(argv)
    sets = list(a.set) + ([f"output.dir={a.out}"] if a.out else [])
    try:
        cfg = C.load(a.config, sets)
        if a.check:
            ags = {side: make_agent(C.agent_spec(cfg["players"][side]), seed=0) for side in ("A", "B")}
            print(C.dump_yaml(C.redacted(cfg)))
            print("配置没有问题。", file=sys.stderr)
            bad = False
            if not a.no_connect:
                from .agents.llm.agent import LLMAgent, check_connection, format_check
                for side, ag in ags.items():
                    if isinstance(ag, LLMAgent):
                        print(f"\n连通性自检 {side}：", file=sys.stderr)
                        d = check_connection(ag)
                        print(format_check(d), file=sys.stderr)
                        bad = bad or not d.get("ok")
            for ag in ags.values():
                ag.close()
            if bad:
                sys.exit(1)
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
