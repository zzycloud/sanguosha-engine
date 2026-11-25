"""用题库考一个模型（或任何智能体）：每道题按配方摆回原局面，让它选，看选的是不是“可接受”的选项。

    python -m sgs_data.evalbank configs/data/evalbank.yaml

配置：
    bank: runs/data-demo-xxx/test.jsonl     # 造数据时生成的题库
    player: {include: ../models/qwen-local.yaml}   # 和对战配置的 players.X 写法一样；也可以写 rule / rule+
    concurrency: 8
    out: runs/evalbank-{name}-{time}

“可接受”= 模拟里没有明显比最好的差（差不多的选项都算对）。输出 results.md：
总正确率、每个模板的正确率、每种答案标签（正例 / 反例）的正确率，以及模型实际选的答案分布——
如果一个模板里模型几乎总选同一种答案、而题目答案是两种都有，就说明它在套路化（学偏了）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List

from sgs import config as C
from sgs.agents.base import decide

from .groups import group_of
from .pipeline import rebuild
from .progress import Progress

DEFAULTS = {"name": None, "bank": None, "player": "rule+", "concurrency": 4, "out": "runs/evalbank-{name}-{time}",
            "limit": None}


def load(path=None, sets=(), data=None):
    raw = C.read_file(path) if path else {}
    if data:
        raw = C.merge(raw, data)
    raw = C.apply_overrides(raw, sets)
    bad = [k for k in raw if k not in DEFAULTS]
    if bad:
        raise C.ConfigError(f"不认识的字段：{', '.join(map(str, bad))}；可用：{', '.join(DEFAULTS)}")
    cfg = C.merge(DEFAULTS, raw)
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    cfg["_base_dir"] = base
    cfg["name"] = cfg["name"] or (os.path.splitext(os.path.basename(path))[0] if path else "evalbank")
    if not cfg["bank"]:
        raise C.ConfigError("要写 bank：题库文件（造数据输出目录里的 test.jsonl）")
    if not os.path.isabs(cfg["bank"]) and not os.path.exists(cfg["bank"]):
        alt = os.path.join(base, cfg["bank"])
        if os.path.exists(alt):
            cfg["bank"] = alt
    return cfg


def run(cfg, stream=None) -> Dict[str, Any]:
    from sgs.agents.registry import make_agent
    from . import policy as _p  # noqa: F401
    from .pipeline import _worker_init
    _worker_init()
    stream = stream or sys.stderr
    with open(cfg["bank"], encoding="utf-8") as f:
        bank = [json.loads(x) for x in f if x.strip()]
    if cfg["limit"]:
        bank = bank[:int(cfg["limit"])]
    spec = C.agent_spec(C.resolve_player("player", cfg["player"], cfg["_base_dir"]))
    agents: Dict[int, Any] = {}

    def one(it):
        tid = threading.get_ident()
        if tid not in agents:
            agents[tid] = make_agent(spec, seed=0)
        g = rebuild(it["recipe"])
        i = decide(agents[tid], g)
        gi = group_of(it["groups"], i)
        return {"id": it["id"], "scenario": it["scenario"], "title": it.get("title"), "tag": it["tag"],
                "chosen": i, "chosen_group": gi, "chosen_label": it["group_labels"][gi],
                "chosen_tag": (it.get("tags") or it["group_labels"])[gi],
                "correct": gi in it["acceptable"], "loss": it["diffs"][gi], "variant": it.get("variant")}

    prog = Progress("题库评测", len(bank), stream)
    results: List[Dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max(1, int(cfg["concurrency"]))) as ex:
        for r in ex.map(one, bank):
            results.append(r)
            prog.tick({"status": "答对" if r["correct"] else "答错"})
    prog.done()
    for a in agents.values():
        a.close()
    top = cfg["out"].replace("{time}", time.strftime("%Y%m%d-%H%M%S")).replace("{name}", cfg["name"])
    os.makedirs(top, exist_ok=True)
    with open(os.path.join(top, "results.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    text = report(results, spec)
    with open(os.path.join(top, "results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, file=stream)
    print(f"→ {top}", file=stream)
    return {"dir": top, "results": results, "report": text}


def report(results, spec) -> str:
    def acc(rs):
        return f"{sum(r['correct'] for r in rs)}/{len(rs)}（{sum(r['correct'] for r in rs) / max(1, len(rs)):.0%}）"
    name = spec if isinstance(spec, str) else spec.get("label") or spec.get("model") or spec.get("kind")
    lines = [f"# 题库评测：{name}", "", f"总正确率 {acc(results)}；平均每题比最好的选项少赢 "
             f"{sum(r['loss'] for r in results) / max(1, len(results)):.1%}。", "",
             "| 模板 | 正确率 | 按题目答案分的正确率 | 模型选的答案分布 |", "|---|---|---|---|"]
    by = defaultdict(list)
    for r in results:
        by[(r["scenario"], r["title"])].append(r)
    for (sc, title), rs in sorted(by.items()):
        bt = defaultdict(list)
        for r in rs:
            bt[r["tag"]].append(r)
        per = "，".join(f"答案是{t}：{acc(v)}" for t, v in sorted(bt.items()))
        chosen = Counter(r["chosen_tag"] for r in rs)
        lines.append(f"| {title}（{sc}） | {acc(rs)} | {per} | " + "，".join(f"{k} {v}" for k, v in chosen.most_common()) + " |")
    lines += ["", "看法：某个模板里“答案是 X”的题正确率高、“答案是 Y”的题正确率低，同时模型几乎总选 X，"
              "说明它学成了套路（只会一种打法），要加这类反例或降低这个模板的比例。"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="用题库考一个模型")
    ap.add_argument("config")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config, a.set)
    except (C.ConfigError, OSError) as e:
        print(f"配置有问题：{e}", file=sys.stderr)
        sys.exit(2)
    run(cfg)


if __name__ == "__main__":
    main()
