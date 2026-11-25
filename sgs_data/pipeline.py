"""造数据的主流程：读配置 → 关键局面（训练 + 题库）→ 普通局面 → （可选）强模型裁判 → 配比 → 导出。

    python -m sgs_data configs/data/demo.yaml
    python -m sgs_data configs/data/demo.yaml --set key.count=20      # 覆盖配置里的某一项

输出目录里：
  items/key-train.jsonl  items/key-test.jsonl  items/normal.jsonl   每道题的全部信息（配方、局面摘要、每组选项的胜率……）
  sft.jsonl              监督微调：messages（和推理时同一套提示词）+ 答案
  dpo.jsonl              偏好对：同一个提示词，好答案和明显更差的答案
  test.jsonl             题库（和训练题用不同的种子），给 python -m sgs_data.evalbank 用
  review.md              给人看：题库每道题的局面、各选项胜率、答案；裁判和模拟意见不一致的题
  stats.md               每类题有多少、留下了多少、答案分布、最后的配比
  config.yaml            这次用的配置
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from sgs import config as C

from . import mix
from .board import BuildFailed, summary
from .label import DEFAULT_ROLLOUT, evaluate, make_policy, policy_is_llm
from .progress import Progress

DEFAULTS: Dict[str, Any] = {
    "name": "data",
    "out": "runs/data-{name}-{time}",
    "workers": 4,                      # 并行进程数（模拟者是模型时是并发线程数）
    "seed": 0,                         # 训练题的起始种子
    "prompt": {"name": "v2", "args": {}},    # 写进 sft/dpo 的提示词模板，要和你推理时用的一样
    "rollout": dict(DEFAULT_ROLLOUT),
    "key": {
        "enabled": True,
        "scenarios": {},               # 模板名: 题数，或 模板名: {count: 题数, params: {...}}
        "test_per_scenario": 20,       # 每个模板出多少道题库题（种子和训练题分开）
        "test_seed": 900000,
        "test_rollout": {"n_min": 64, "n_max": 256},   # 题库题模拟得更多，答案更准
    },
    "normal": {
        "enabled": True,
        "games": 50,                   # 打多少局来抽题
        "per_game": 6,                 # 每局抽几个决策点
        "players": {"A": "rule+", "B": "rule+"},       # 谁来打这些局（可以换成你的模型）
        "general_pool": None,          # 武将池（默认全部标准武将）
        "focus": None,                 # 只抽这些武将的决策（例如 [郭嘉, 诸葛亮]），null 不限
        "skip_kinds": [],
        "seed": 500000,
        "rollout": {"n_min": 16, "n_max": 64},
    },
    "judge": {
        "enabled": False,
        "player": None,                # 强模型的配置（和对战配置里的 players.X 写法一样，可以 include）
        "votes": 5,
        "scope": "confident",          # confident：只问模拟有结论的题；all：全问
        "on_disagree": "review",       # review：不进训练、写进 review.md；drop：扔掉；keep：照样用
        "concurrency": 8,
    },
    "mix": dict(mix.DEFAULTS),
    "progress": {"every": 2.0},        # 每隔几秒打印一行进度
}


# ---------------------------------------------------------------------- 配置
def load(path: Optional[str] = None, sets=(), data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw: Dict[str, Any] = C.read_file(path) if path else {}
    if data:
        raw = C.merge(raw, data)
    raw = C.apply_overrides(raw, sets)
    bad = [k for k in raw if k not in DEFAULTS]
    if bad:
        raise C.ConfigError(f"配置顶层有不认识的字段：{', '.join(map(str, bad))}；可用：{', '.join(DEFAULTS)}")
    for sec in ("rollout", "key", "normal", "judge", "mix"):
        if raw.get(sec) is not None and not isinstance(raw[sec], dict):
            raise C.ConfigError(f"{sec} 要写成一个配置段")
        bad = [k for k in (raw.get(sec) or {}) if k not in DEFAULTS[sec]]
        if bad:
            raise C.ConfigError(f"{sec} 里有不认识的字段：{', '.join(map(str, bad))}；可用：{', '.join(DEFAULTS[sec])}")
    cfg = C.merge(DEFAULTS, raw)
    if path and not raw.get("name"):
        cfg["name"] = os.path.splitext(os.path.basename(path))[0]
    cfg["_base_dir"] = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    from .scenarios import all_scenarios
    names = all_scenarios()
    for n in (cfg["key"]["scenarios"] or {}):
        if n not in names:
            raise C.ConfigError(f"key.scenarios 里的 {n} 不是已有的模板；有：{', '.join(sorted(names))}")
    if cfg["judge"]["enabled"] and not cfg["judge"]["player"]:
        raise C.ConfigError("judge.enabled 打开时要写 judge.player（强模型的配置）")
    return cfg


def out_dir(cfg) -> str:
    return cfg["out"].replace("{time}", time.strftime("%Y%m%d-%H%M%S")).replace("{name}", cfg["name"])


# ---------------------------------------------------------------------- 在子进程里做的事
_POLICIES: Dict[str, Any] = {}
_PROMPTS: Dict[str, Any] = {}


def _policy(spec, base_dir):
    key = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str) + base_dir
    if key not in _POLICIES:
        _POLICIES[key] = make_policy(spec, base_dir)
    return _POLICIES[key]


def _prompt(pcfg):
    from sgs.agents.llm.prompt import make_prompt
    key = json.dumps(pcfg, sort_keys=True, ensure_ascii=False)
    if key not in _PROMPTS:
        _PROMPTS[key] = make_prompt(pcfg.get("name", "v2"), **(pcfg.get("args") or {}))
    return _PROMPTS[key]


def _finish_item(g, item: Dict[str, Any], rollout: Dict[str, Any], policy_spec, base_dir, pcfg, seed) -> Dict[str, Any]:
    d = g.pending
    me = d.player
    view = g.view(me, with_logs=True, derived=True)
    item.update(seat=me, general=g.players[me].general, opponent=g.players[1 - me].general,
                turn=g.turn_count, phase=g.phase, kind=d.kind, prompt_text=d.prompt,
                legal_actions=[o.label for o in d.options],
                messages=_prompt(pcfg).messages(view, d), summary=summary(g, me))
    ev = evaluate(g, _policy(policy_spec, base_dir), rollout, seed=seed)
    item.update(ev)
    item["best_action"] = ev["groups"][ev["best"]][0]
    return item


def key_task(t: Dict[str, Any]) -> Dict[str, Any]:
    from .scenarios import make_scenario
    sc = make_scenario(t["scenario"], t.get("params"))
    try:
        g, var, seed = sc.build_retry(t["seed"])
    except BuildFailed as e:
        return {"failed": str(e), "scenario": t["scenario"], "split": t["split"]}
    item = {"format": "sgs-train-item/1", "id": t["id"], "source": "key", "split": t["split"],
            "scenario": sc.name, "title": sc.title, "variant": var,
            "recipe": {"type": "scenario", "name": sc.name, "params": t.get("params") or {}, "seed": seed}}
    try:
        item = _finish_item(g, item, t["rollout"], t["policy"], t["base_dir"], t["prompt"], seed)
    except Exception as e:  # 一道题出错不拖垮整批，记到 items/failed.jsonl
        return {"failed": f"{type(e).__name__}: {e}", "id": t["id"], "recipe": item["recipe"]}
    item["tag"] = sc.tag(item["group_labels"][item["best"]])
    item["tags"] = [sc.tag(lb) for lb in item["group_labels"]]
    return item


def normal_task(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    from sgs.agents.registry import make_agent
    from . import policy as _p  # noqa: F401
    from .normal import play_and_sample, replay
    agents = [make_agent(C.agent_spec(t["players"][k]), seed=t["seed"] * 2 + j) for j, k in enumerate(("A", "B"))]
    if t["seed"] % 2:
        agents = agents[::-1]          # 换边：奇数种子交换先后手
    recipes = play_and_sample(t["seed"], agents, t["per_game"], t.get("pool"), t.get("focus"), t.get("skip_kinds") or ())
    for a in agents:
        a.close()
    out = []
    for j, r in enumerate(recipes):
        g = replay(r)
        item = {"format": "sgs-train-item/1", "id": f"normal-{t['seed']}-{j}", "source": "normal", "split": "train",
                "scenario": None, "variant": {}, "recipe": r}
        try:
            item = _finish_item(g, item, t["rollout"], t["policy"], t["base_dir"], t["prompt"], t["seed"] * 100 + j)
        except Exception as e:
            out.append({"failed": f"{type(e).__name__}: {e}", "id": item["id"], "recipe": r})
            continue
        item["tag"] = item["group_labels"][item["best"]]
        item["tags"] = list(item["group_labels"])
        out.append(item)
    return out


def rebuild(recipe: Dict[str, Any]):
    """按配方重新摆出决策点（题库评测、裁判用）。"""
    if recipe["type"] == "scenario":
        from .scenarios import make_scenario
        g, _ = make_scenario(recipe["name"], recipe.get("params")).build(recipe["seed"])
        return g
    from .normal import replay
    return replay(recipe)


# ---------------------------------------------------------------------- 调度
def _executor(workers: int, threads: bool):
    if workers <= 1:
        return None
    if threads:
        return cf.ThreadPoolExecutor(workers)
    import multiprocessing as mp
    # 统一用 spawn：macOS / Windows 默认就是它；Linux 上 fork 带线程的进程可能卡死
    return cf.ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"), initializer=_worker_init)


def _worker_init():
    from sgs.core.registry import load_all
    from . import policy as _p  # noqa: F401  注册 rule+
    load_all()


def run_tasks(fn, tasks: List[Dict[str, Any]], workers: int, threads: bool, prog: Progress, sink) -> None:
    ex = _executor(workers, threads)
    if ex is None:
        for t in tasks:
            res = fn(t)
            for r in (res if isinstance(res, list) else [res]):
                sink(r)
                prog.tick(r)
            if isinstance(res, list) and not res:
                prog.tick(None)
        prog.done()
        return
    with ex:
        futs = [ex.submit(fn, t) for t in tasks]
        try:
            for f in cf.as_completed(futs):
                res = f.result()
                for r in (res if isinstance(res, list) else [res]):
                    sink(r)
                    prog.tick(r)
                if isinstance(res, list) and not res:
                    prog.tick(None)
        except KeyboardInterrupt:
            for f in futs:
                f.cancel()
            raise
    prog.done()


class JsonlWriter:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.f = open(path, "w", encoding="utf-8")
        self.items: List[Dict[str, Any]] = []

    def __call__(self, item):
        if item is None:
            return
        if item.get("failed"):
            with open(os.path.join(os.path.dirname(self.path), "failed.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            return
        self.items.append(item)
        self.f.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def run(cfg: Dict[str, Any], stream=None) -> Dict[str, Any]:
    _worker_init()
    stream = stream or sys.stderr
    top = out_dir(cfg)
    os.makedirs(top, exist_ok=True)
    with open(os.path.join(top, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(C.dump_yaml(C.redacted(cfg)))
    base_dir = cfg["_base_dir"]
    pol = cfg["rollout"]["policy"]
    threads = policy_is_llm(pol)
    workers = int(cfg["workers"])
    every = float(cfg["progress"]["every"])
    print(f"造数据：{cfg['name']} → {top}", file=stream)
    print(f"  模拟者：{pol if not isinstance(pol, dict) else pol.get('label') or pol.get('include') or pol.get('model')}"
          f"，每组选项 {cfg['rollout']['n_min']}～{cfg['rollout']['n_max']} 个随机世界，"
          f"{workers} 个{'线程' if threads else '进程'}", file=stream)
    sets: Dict[str, List[Dict[str, Any]]] = {}

    common = {"policy": pol, "base_dir": base_dir, "prompt": cfg["prompt"]}
    kc = cfg["key"]
    if kc["enabled"] and kc["scenarios"]:
        for split in ("train", "test"):
            tasks = []
            for name, spec in kc["scenarios"].items():
                spec = spec if isinstance(spec, dict) else {"count": spec}
                n = int(spec.get("count", 0)) if split == "train" else int(spec.get("test", kc["test_per_scenario"]))
                base = cfg["seed"] if split == "train" else kc["test_seed"]
                ro = cfg["rollout"] if split == "train" else C.merge(cfg["rollout"], kc["test_rollout"])
                for i in range(n):
                    tasks.append(dict(common, scenario=name, params=spec.get("params"), split=split,
                                      seed=base + i, id=f"key-{name}-{split}-{i:05d}", rollout=ro))
            if not tasks:
                continue
            w = JsonlWriter(os.path.join(top, "items", f"key-{split}.jsonl"))
            prog = Progress("关键局面" + ("（训练）" if split == "train" else "（题库）"), len(tasks), stream, every)
            try:
                run_tasks(key_task, tasks, workers, threads, prog, w)
            finally:
                w.close()
            sets[f"key-{split}"] = w.items
    nc = cfg["normal"]
    if nc["enabled"] and nc["games"]:
        players = {k: C.resolve_player(f"normal.players.{k}", v, base_dir) for k, v in nc["players"].items()}
        nthreads = threads or any(policy_is_llm(p) for p in players.values())
        ro = C.merge(cfg["rollout"], nc["rollout"])
        tasks = [dict(common, seed=nc["seed"] + i, per_game=nc["per_game"], players=players, pool=nc["general_pool"],
                      focus=nc["focus"], skip_kinds=nc["skip_kinds"], rollout=ro) for i in range(int(nc["games"]))]
        w = JsonlWriter(os.path.join(top, "items", "normal.jsonl"))
        prog = Progress("普通局面", len(tasks) * int(nc["per_game"]), stream, every)
        try:
            run_tasks(normal_task, tasks, workers, nthreads, prog, w)
        finally:
            w.close()
        sets["normal"] = w.items
    jc = cfg["judge"]
    if jc["enabled"]:
        run_judge(cfg, sets, top, stream)
    res = mix.build(cfg, sets, top)
    print(res["report"], file=stream)
    print(f"\n→ {top}", file=stream)
    return {"dir": top, **res}


def run_judge(cfg, sets, top, stream) -> None:
    from .judge import judge_item, make_judge_agent
    jc = cfg["judge"]
    todo = [it for s in sets.values() for it in s if jc["scope"] == "all" or it["status"] == "confident"]
    if not todo:
        return
    prog = Progress("强模型裁判", len(todo), stream, float(cfg["progress"]["every"]))
    agents: Dict[int, Any] = {}

    def one(it):
        import threading
        tid = threading.get_ident()
        if tid not in agents:
            agents[tid] = make_judge_agent(jc["player"], cfg["_base_dir"])
        it["judge"] = judge_item(agents[tid], rebuild(it["recipe"]), it, int(jc["votes"]))
        return it

    with cf.ThreadPoolExecutor(int(jc["concurrency"])) as ex:
        for it in ex.map(one, todo):
            prog.tick(it)
    prog.done()
    for name, items in sets.items():
        path = os.path.join(top, "items", f"{name}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="造三国杀训练数据：关键局面 + 普通局面，往下模拟打标签，导出 SFT/DPO/题库")
    ap.add_argument("config")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config, a.set)
    except (C.ConfigError, OSError) as e:
        print(f"配置有问题：{e}", file=sys.stderr)
        sys.exit(2)
    try:
        run(cfg)
    except KeyboardInterrupt:
        print("\n中途停了。已经做好的题在 items/ 里。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
