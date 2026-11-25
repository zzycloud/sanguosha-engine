"""对比实验：同一套设置下换一个东西（比如提示词模板），各打若干局，把胜率、答错率、兜底率、token 放在一起比。

    python -m sgs.experiment configs/compare-prompts.yaml
    python -m sgs.experiment configs/compare-prompts.yaml --set base.match.games=20

配置文件：

    name: compare-prompts
    dir: runs/{name}-{time}        # 总输出目录；每个变体一个子目录
    base:                          # 和普通对战配置一样（match / players / output），每个变体在它上面覆盖
      match: {games: 100, swap: true}
      players: {A: {include: models/qwen-local.yaml}, B: rule}
    variants:                      # 按顺序一个一个跑
      old: {players: {A: {prompt: default}}}
      new: {players: {A: {prompt: v2}}}
    side: A                        # 比哪一方的数据（默认 A）

输出：每个变体的目录（和 sgs.arena 一样，有 index.html），总目录里的 comparison.json / comparison.md，终端打印对比表。
胜率附 95% 置信区间：100 局时区间大约 ±10%，两组差距在这以内不太能说明谁更好，可以加局数。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import config as C
from .arena import ArenaError, run_config
from .progress import fmt_secs

EXP_KEYS = {"name", "dir", "base", "variants", "side"}


def wilson(k: int, n: int, z: float = 1.96) -> List[float]:
    """胜率的 95% 置信区间（Wilson 区间）。"""
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def load(path: Optional[str] = None, sets: Sequence[str] = (), data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw: Dict[str, Any] = C.read_file(path) if path else {}
    if data:
        raw = C.merge(raw, data)
    raw = C.apply_overrides(raw, sets)
    bad = [k for k in raw if k not in EXP_KEYS]
    if bad:
        raise C.ConfigError(f"实验配置里有不认识的字段：{', '.join(bad)}；可用：{', '.join(sorted(EXP_KEYS))}")
    if not isinstance(raw.get("variants"), dict) or len(raw["variants"]) < 1:
        raise C.ConfigError("variants 下至少写一个变体，例如 old: {players: {A: {prompt: default}}}")
    raw.setdefault("name", os.path.splitext(os.path.basename(path))[0] if path else "experiment")
    raw.setdefault("dir", "runs/{name}-{time}")
    raw.setdefault("side", "A")
    if raw["side"] not in ("A", "B"):
        raise C.ConfigError("side 只能是 A 或 B")
    base_dir = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    raw["_base_dir"] = base_dir
    out = {}
    for name, over in raw["variants"].items():
        cfg = C.load(data=C.merge(raw.get("base") or {}, over or {}), base_dir=base_dir)
        cfg["name"] = f"{raw['name']}-{name}"
        m = cfg["match"]
        if m["swap"] and m["games"] % 2:
            raise C.ConfigError(f"变体 {name}：开着 swap（换边）时局数要是偶数，现在是 {m['games']}")
        out[name] = cfg
    raw["_configs"] = out
    return raw


def row(name: str, s: Dict[str, Any], side: str) -> Dict[str, Any]:
    v = s["sides"][side]
    other = s["sides"]["B" if side == "A" else "A"]
    L = v.get("llm") or {}
    req = L.get("llm_calls", 0)
    n = s["games"]
    return {
        "variant": name, "agent": v["agent"], "opponent": other["agent"], "games": n, "wins": v["wins"],
        "win_rate": v["win_rate"], "win_rate_ci95": wilson(v["wins"], n),
        "wins_first": v["wins_first"], "games_first": v["games_first"],
        "draws_or_aborted": s["draws_or_aborted"], "errors": s["errors"],
        "decisions": L.get("decisions", 0), "requests": req,
        "illegal": L.get("illegal", 0), "illegal_rate": L.get("illegal_rate", 0.0),
        "fallbacks": L.get("fallbacks", 0), "fallback_rate": L.get("fallback_rate", 0.0),
        "request_errors": L.get("errors", 0),
        "avg_prompt_tokens": round(L.get("prompt_tokens", 0) / req, 1) if req else None,
        "avg_completion_tokens": round(L.get("completion_tokens", 0) / req, 1) if req else None,
        "avg_latency": L.get("avg_latency"), "seconds": s.get("seconds"),
    }


def pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def table(rows: List[Dict[str, Any]]) -> str:
    head = ["变体", "局数", "胜率（95% 区间）", "先手胜", "答错率", "兜底率", "请求失败", "平均提示 token", "平均回复 token", "平均用时"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lo, hi = r["win_rate_ci95"]
        lines.append("| " + " | ".join(str(x) for x in [
            r["variant"], r["games"], f"{pct(r['win_rate'])}（{pct(lo)}～{pct(hi)}）",
            f"{r['wins_first']}/{r['games_first']}", pct(r["illegal_rate"]), pct(r["fallback_rate"]),
            r["request_errors"], r["avg_prompt_tokens"] if r["avg_prompt_tokens"] is not None else "-",
            r["avg_completion_tokens"] if r["avg_completion_tokens"] is not None else "-",
            f"{r['avg_latency']} 秒" if r["avg_latency"] is not None else "-"]) + " |")
    return "\n".join(lines)


def run(exp: Dict[str, Any], stream=None) -> Dict[str, Any]:
    stream = stream or sys.stderr
    top = exp["dir"].replace("{time}", time.strftime("%Y%m%d-%H%M%S")).replace("{name}", exp["name"])
    os.makedirs(top, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    names = list(exp["_configs"])
    for k, (name, cfg) in enumerate(exp["_configs"].items(), 1):
        cfg = dict(cfg, output=dict(cfg["output"], dir=os.path.join(top, name)))
        print(f"\n===== 变体 {k}/{len(names)}：{name} =====", file=stream, flush=True)
        s = run_config(cfg)
        rows.append(row(name, s, exp["side"]))
        _write(top, exp, rows)
        if s.get("interrupted"):
            print("中途按了 Ctrl+C，后面的变体不跑了。", file=stream)
            break
    return {"dir": top, "rows": rows}


def _write(top: str, exp: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    with open(os.path.join(top, "comparison.json"), "w", encoding="utf-8") as f:
        json.dump({"name": exp["name"], "side": exp["side"], "rows": rows}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(top, "comparison.md"), "w", encoding="utf-8") as f:
        f.write(f"# {exp['name']}\n\n比的是 {exp['side']} 方（{rows[0]['agent']}，对手 {rows[0]['opponent']}）。"
                "答错率 = 答错次数 / 请求次数；兜底率 = 兜底次数 / 模型决策次数；token 是每次请求的平均值。\n\n")
        f.write(table(rows) + "\n\n")
        for r in rows:
            f.write(f"- {r['variant']}：[{r['variant']}/index.html]({r['variant']}/index.html)\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="对比实验：每个变体各打一轮，比胜率、答错率、兜底率、token")
    ap.add_argument("config")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值",
                    help="覆盖实验配置，例如 --set base.match.games=20 --set base.players.A.model=sgs")
    a = ap.parse_args(argv)
    try:
        exp = load(a.config, a.set)
    except (C.ConfigError, KeyError, TypeError, OSError) as e:
        print(f"配置有问题：{e}", file=sys.stderr)
        sys.exit(2)
    try:
        res = run(exp)
    except ArenaError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已退出。", file=sys.stderr)
        sys.exit(130)
    print()
    print(table(res["rows"]))
    print(f"\n→ {os.path.join(res['dir'], 'comparison.md')}")


if __name__ == "__main__":
    main()
