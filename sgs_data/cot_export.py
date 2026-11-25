"""把强模型的对局记录（llm_calls.json）导出成“三行短分析 + 编号”的 SFT 数据。

    python -m sgs_data.cot_export configs/data/cot-export.yaml
    python -m sgs_data.cot_export configs/data/cot-export.yaml --set overlong=compress

分析从哪来（analysis_from）：
  content    对局时 cot_source: content，模型在回答里写的三行分析。按字数过滤：超长的丢掉（默认），
             或者交给压缩模型改写成三行（overlong: compress），或者照样留（keep）。
  reasoning  对局时 cot_source: reasoning，模型思考字段（reasoning / reasoning_content）里的原文。
             原文都很长，一律交给压缩模型改成三行；压不好的丢掉。
  auto       （默认）每条按它对局时的 cot_source 来。
导出时提示词会换成“先写三行分析再给编号”的格式（和 cot: key 推理时一样），答案是
  局面：…\\n得失：…\\n结论：…\\n{"action": k}
思考字段的原文只用来压缩，本身不进训练数据。

输出目录：sft.jsonl（messages + 答案）、rows.jsonl（每条的来源、原分析、压缩结果）、stats.md、config.yaml
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from sgs import config as C
from sgs.agents.llm.addons import (DEFAULT_MAX_CHARS, analysis_len, three_lines, to_cot_messages, visible_text,
                                   _clean_analysis)

DEFAULTS: Dict[str, Any] = {
    "name": "cot-export",
    "out": "runs/cot-sft-{name}-{time}",
    "runs": [],                  # 对局输出目录（可以写通配符），会找下面所有 games/*/llm_calls.json
    "agents": [],                # 只要这些 label 的智能体（空 = 全要）
    "only_wins": False,          # 只要赢了的那一方的决策
    "analysis_from": "auto",     # auto / content / reasoning（见文件开头）
    "max_chars": DEFAULT_MAX_CHARS,   # 三行分析加起来最多多少字（去掉空白，含“局面：”这些标签）
    "overlong": "drop",          # content 分析超长时：drop 丢掉 / compress 用压缩模型改成三行 / keep 照样留
    "require_three_lines": True,  # content 分析不是标准三行时当作不合格（按 overlong 处理）
    "compressor": None,          # 压缩用的模型，写法和 players.A 一样（例如 include: ../models/strong-api.yaml）
    "compress_retries": 1,       # 压缩结果不合格（超长或不是三行）时再压几次
    "concurrency": 4,            # 压缩请求的并发数
    "include_plain": False,      # 非关键决策（没有分析的）也导出，答案只有编号
    "drop_tips": False,          # 把对局时注入的武将技巧从提示词里删掉
    "cot_mode": "key",           # 导出提示词用的格式说明：key / all，和你推理时的 cot 设置一样
    "progress": {"every": 2.0},
}

COMPRESS_PROMPT = """下面是一位三国杀玩家做一个决定时的思考过程，和他最后的选择。
把思考压缩成三行，每行一句话，三行加起来不超过 {n} 字（不算空白，含标签）：
局面：最关键的局面信息
得失：几个选项各自的主要得失
结论：选哪个（必须和他最后的选择一致）
只输出这三行，不要别的。

可选的动作：
{labels}
他最后选的：{choice}

思考过程：
{text}"""


def load(path: Optional[str] = None, sets=(), data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw: Dict[str, Any] = C.read_file(path) if path else {}
    if data:
        raw = C.merge(raw, data)
    raw = C.apply_overrides(raw, sets)
    bad = [k for k in raw if k not in DEFAULTS]
    if bad:
        raise C.ConfigError(f"配置里有不认识的字段：{', '.join(map(str, bad))}；可用：{', '.join(DEFAULTS)}")
    cfg = C.merge(DEFAULTS, raw)
    if path and not raw.get("name"):
        cfg["name"] = os.path.splitext(os.path.basename(path))[0]
    cfg["_base_dir"] = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    if cfg["analysis_from"] not in ("auto", "content", "reasoning"):
        raise C.ConfigError("analysis_from 只能是 auto / content / reasoning")
    if cfg["overlong"] not in ("drop", "compress", "keep"):
        raise C.ConfigError("overlong 只能是 drop / compress / keep")
    if isinstance(cfg["runs"], str):
        cfg["runs"] = [cfg["runs"]]
    if not cfg["runs"]:
        raise C.ConfigError("runs 要写至少一个对局输出目录")
    return cfg


def _out_dir(cfg) -> str:
    return cfg["out"].replace("{time}", time.strftime("%Y%m%d-%H%M%S")).replace("{name}", cfg["name"])


def find_games(cfg) -> List[str]:
    dirs: List[str] = []
    for pat in cfg["runs"]:
        p = pat if os.path.isabs(pat) else os.path.join(os.getcwd(), pat)
        for d in sorted(glob.glob(p)) or [p]:
            dirs += sorted(glob.glob(os.path.join(d, "**", "llm_calls.json"), recursive=True))
    return sorted(set(dirs))


def _winner(path: str) -> Optional[int]:
    try:
        with open(os.path.join(os.path.dirname(path), "result.json"), encoding="utf-8") as f:
            return json.load(f).get("winner_seat")
    except (OSError, ValueError):
        return None


def _answer(analysis: Optional[str], action: int) -> str:
    tail = json.dumps({"action": action}, ensure_ascii=False)
    return f"{analysis}\n{tail}" if analysis else tail


def collect(cfg) -> List[Dict[str, Any]]:
    """读出所有候选决策（还没压缩、没过滤）。"""
    rows: List[Dict[str, Any]] = []
    for path in find_games(cfg):
        with open(path, encoding="utf-8") as f:
            calls = json.load(f)
        win = _winner(path)
        for c in calls:
            if c.get("source") != "llm" or not c.get("attempts"):
                continue
            if cfg["agents"] and c.get("agent") not in cfg["agents"]:
                continue
            if cfg["only_wins"] and (win is None or c.get("seat") != win):
                continue
            ok = next((a for a in reversed(c["attempts"]) if a.get("parsed") is not None and not a.get("error")), None)
            if ok is None:
                continue
            src = (c.get("addons") or {}).get("cot_source") or c.get("cot_source") or "content"
            frm = cfg["analysis_from"] if cfg["analysis_from"] != "auto" else src
            rel = os.path.relpath(path)
            row = {"file": path if rel.startswith("..") else rel, "game_id": c.get("game_id"), "call": c.get("call"),
                   "seat": c.get("seat"), "agent": c.get("agent"), "kind": c.get("kind"),
                   "cot": bool(c.get("cot")), "from": frm, "action": c["action"],
                   "labels": c.get("legal_actions") or [], "messages": c["attempts"][0]["messages"],
                   "tips_text": c.get("tips_text"), "won": None if win is None else c.get("seat") == win}
            if row["cot"]:
                if frm == "reasoning":
                    row["source_text"] = (ok.get("reasoning") or "").strip() or None
                else:
                    a = c.get("analysis") or ok.get("analysis")
                    row["source_text"] = _clean_analysis(visible_text(a)) if a else None
            elif not cfg["include_plain"]:
                continue
            rows.append(row)
    return rows


def _compressor(cfg):
    from sgs.agents.registry import make_agent
    if not cfg["compressor"]:
        raise C.ConfigError("要压缩分析（overlong: compress，或分析来自 reasoning）时，要写 compressor（压缩用的模型）")
    p = C.resolve_player("compressor", cfg["compressor"], cfg["_base_dir"])
    p.setdefault("fallback", "none")
    return make_agent(C.agent_spec(p))


def compress(agent, row: Dict[str, Any], max_chars: int, retries: int) -> Dict[str, Any]:
    labels = "\n".join(f"{i}. {x}" for i, x in enumerate(row["labels"]))
    choice = row["labels"][row["action"]] if row["action"] < len(row["labels"]) else str(row["action"])
    msgs = [{"role": "user", "content": COMPRESS_PROMPT.format(n=max_chars, labels=labels, choice=choice,
                                                               text=row["source_text"])}]
    tries = []
    for _ in range(retries + 1):
        try:
            r = agent.client.chat(msgs, **agent.params)
        except Exception as e:  # 网络或服务出错，这条丢掉
            tries.append({"error": str(e)[:300]})
            break
        out = _clean_analysis(visible_text(r.text or ""))
        good = three_lines(out) and analysis_len(out) <= max_chars
        tries.append({"text": r.text, "chars": analysis_len(out), "three_lines": three_lines(out)})
        if good:
            return {"ok": True, "analysis": out, "tries": tries}
        msgs = msgs + [{"role": "assistant", "content": r.text or ""},
                       {"role": "user", "content": f"不合格：要正好三行（局面/得失/结论），加起来不超过 {max_chars} 字。"
                                                   f"你写了 {analysis_len(out)} 字。重写，只输出三行。"}]
    return {"ok": False, "tries": tries}


def decide_rows(cfg, rows: List[Dict[str, Any]], stream=sys.stderr) -> List[Dict[str, Any]]:
    """给每条定下最终分析，或标上丢掉的原因。"""
    n, need = int(cfg["max_chars"]), []
    for r in rows:
        if not r["cot"]:
            r["analysis"], r["status"] = None, "plain"
            continue
        t = r.get("source_text")
        if not t:
            r["status"] = "no_text"
            continue
        if r["from"] == "reasoning":
            need.append(r)
            continue
        good = analysis_len(t) <= n and (three_lines(t) or not cfg["require_three_lines"])
        if good:
            r["analysis"], r["status"] = t, "ok"
        elif cfg["overlong"] == "keep":
            r["analysis"], r["status"] = t, "kept_overlong"
        elif cfg["overlong"] == "drop":
            r["status"] = "dropped_overlong"
        else:
            need.append(r)
    if need:
        agent = _compressor(cfg)
        every = float((cfg.get("progress") or {}).get("every", 2.0))
        t0 = last = time.perf_counter()
        print(f"\n[压缩分析] 开始，共 {len(need)} 条", file=stream, flush=True)
        done = 0
        with cf.ThreadPoolExecutor(max(1, int(cfg["concurrency"]))) as ex:
            futs = {ex.submit(compress, agent, r, n, int(cfg["compress_retries"])): r for r in need}
            for fu in cf.as_completed(futs):
                r, res = futs[fu], fu.result()
                r["compress"] = res
                if res["ok"]:
                    r["analysis"], r["status"] = res["analysis"], "compressed"
                else:
                    r["status"] = "compress_failed"
                done += 1
                now = time.perf_counter()
                if now - last >= every or done == len(need):
                    last = now
                    ok = sum(1 for x in need if x.get("status") == "compressed")
                    print(f"[压缩分析] {done}/{len(need)} 用时 {now - t0:.0f} 秒，压缩成功 {ok}", file=stream, flush=True)
        agent.close()
    return rows


STATUS_ZH = {"ok": "原分析合格", "compressed": "压缩后合格", "kept_overlong": "超长但照样留", "plain": "无分析（只有编号）",
             "dropped_overlong": "超长丢掉", "compress_failed": "压缩失败丢掉", "no_text": "没有分析文本丢掉"}
KEEP = ("ok", "compressed", "kept_overlong", "plain")


def run(cfg, stream=sys.stderr) -> Dict[str, Any]:
    out = _out_dir(cfg)
    os.makedirs(out, exist_ok=True)
    rows = collect(cfg)
    print(f"[导出] 找到 {len(rows)} 条候选决策", file=stream, flush=True)
    decide_rows(cfg, rows, stream)
    kept = [r for r in rows if r.get("status") in KEEP]
    with open(os.path.join(out, "sft.jsonl"), "w", encoding="utf-8") as f:
        for r in kept:
            msgs = r["messages"]
            drop = r["tips_text"] if cfg["drop_tips"] else None
            if r["cot"]:
                msgs = to_cot_messages(msgs, cfg["cot_mode"], int(cfg["max_chars"]), drop_tips=drop)
            elif drop:
                msgs = to_cot_messages(msgs, cfg["cot_mode"], int(cfg["max_chars"]), drop_tips=drop)
                msgs[-1] = r["messages"][-1]          # 非关键决策：用户消息保持“只给编号”
            f.write(json.dumps({"messages": msgs + [{"role": "assistant", "content": _answer(r["analysis"], r["action"])}],
                                "meta": {"game": r["file"], "call": r["call"], "agent": r["agent"], "from": r["from"],
                                         "status": r["status"], "won": r["won"]}}, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "rows.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if k != "messages"}, ensure_ascii=False) + "\n")
    cnt = Counter(r.get("status") for r in rows)
    chars = [analysis_len(r["analysis"]) for r in kept if r.get("analysis")]
    lines = [f"# 导出统计：{cfg['name']}", "",
             f"候选 {len(rows)} 条，导出 {len(kept)} 条（字数上限 {cfg['max_chars']}，超长处理 {cfg['overlong']}，"
             f"分析来源 {cfg['analysis_from']}）。", "", "| 情况 | 条数 |", "|---|---|"]
    lines += [f"| {STATUS_ZH.get(k, k)} | {v} |" for k, v in cnt.most_common()]
    if chars:
        lines += ["", f"导出的分析平均 {sum(chars) / len(chars):.0f} 字，最长 {max(chars)} 字。"]
    with open(os.path.join(out, "stats.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(C.dump_yaml(C.redacted({k: v for k, v in cfg.items() if not k.startswith("_")})))
    print(f"[导出] 完成：导出 {len(kept)} / {len(rows)} 条 → {out}", file=stream, flush=True)
    return {"dir": out, "rows": rows, "kept": len(kept), "counts": dict(cnt)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="把对局记录导出成“三行短分析 + 编号”的 SFT 数据")
    ap.add_argument("config")
    ap.add_argument("--set", action="append", default=[], help="覆盖配置项，例如 --set overlong=compress")
    a = ap.parse_args(argv)
    run(load(a.config, a.set))


if __name__ == "__main__":
    main()
