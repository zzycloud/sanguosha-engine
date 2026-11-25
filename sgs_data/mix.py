"""配比和导出：决定哪些题进训练、各占多少，写 sft.jsonl / dpo.jsonl / test.jsonl / review.md / stats.md。

规则（都能在配置的 mix 段改）：
1. 只用“有结论”的题：至少有一个选项明显更差。选什么都差不多的题学了没用，扔掉。
2. 开了裁判时，裁判和模拟意见不一致的题按 judge.on_disagree 处理（默认不进训练、写进 review.md）。
3. 关键局面每个模板内部按答案标签平衡：任何一种答案最多占 max_tag_share（默认 70%），
   这样模型不会学成“见了郭嘉就卖血”——反例（答案反过来的题）一定在里面。
4. 普通局面每个武将最多占 max_general_share。
5. 最后按 key_share 配比关键局面和普通局面（默认关键 30%、普通 70%），多的一边随机抽掉。
"""
from __future__ import annotations

import json
import math
import os
import random
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

DEFAULTS: Dict[str, Any] = {
    "key_share": 0.3,          # 关键局面在训练集里占多少
    "max_tag_share": 0.7,      # 每个模板里任何一种答案最多占多少
    "max_general_share": 0.25, # 普通局面里每个武将最多占多少
    "dpo_pairs_per_item": 2,   # 每道题最多出几个偏好对（好答案 vs 每个明显更差的答案，差得多的优先）
    "review_limit": 200,       # review.md 最多列多少道题
    "seed": 0,
}


def usable(it: Dict[str, Any], on_disagree: str, judge_on: bool) -> str:
    """ok / tie / review / drop"""
    if it.get("status") != "confident":
        return "tie"
    j = it.get("judge")
    if judge_on and j is not None and j.get("agree") is False:
        return {"keep": "ok", "drop": "drop"}.get(on_disagree, "review")
    return "ok"


def cap_tags(items: List[Dict[str, Any]], share: float, rng: random.Random) -> List[Dict[str, Any]]:
    by = defaultdict(list)
    for it in items:
        by[it["tag"]].append(it)
    if len(by) < 2 or share >= 1:
        return items
    while True:
        total = sum(len(v) for v in by.values())
        tag, big = max(by.items(), key=lambda kv: len(kv[1]))
        rest = total - len(big)
        limit = int(math.floor(share / (1 - share) * rest))
        if len(big) <= max(limit, 1) or rest == 0:
            break
        rng.shuffle(big)
        by[tag] = big[:max(limit, 1)]
    return [it for v in by.values() for it in v]


def cap_generals(items: List[Dict[str, Any]], share: float, rng: random.Random) -> List[Dict[str, Any]]:
    if not items or share >= 1:
        return items
    by = defaultdict(list)
    for it in items:
        by[it["general"]].append(it)
    if len(by) * share < 1:          # 武将太少时上限放宽到平均
        share = 1 / len(by)
    limit = max(1, int(share * len(items)))
    out = []
    for v in by.values():
        rng.shuffle(v)
        out.extend(v[:limit])
    return out


def sample(items, k, rng):
    items = list(items)
    rng.shuffle(items)
    return items[:k]


def answer(it: Dict[str, Any], group: int) -> str:
    return json.dumps({"action": it["groups"][group][0]}, ensure_ascii=False)


def meta(it):
    return {k: it.get(k) for k in ("id", "source", "scenario", "general", "opponent", "kind", "tag", "variant",
                                    "group_labels", "values", "n")}


def build(cfg: Dict[str, Any], sets: Dict[str, List[Dict[str, Any]]], top: str) -> Dict[str, Any]:
    mc = dict(DEFAULTS, **(cfg.get("mix") or {}))
    jc = cfg.get("judge") or {}
    rng = random.Random(mc["seed"])
    judge_on = bool(jc.get("enabled"))
    od = jc.get("on_disagree", "review")
    train_key = sets.get("key-train", [])
    test = sets.get("key-test", [])
    normal = sets.get("normal", [])
    status = {"key": Counter(), "normal": Counter(), "test": Counter()}
    keep_key, keep_normal, review = [], [], []
    for src, items, keep in (("key", train_key, keep_key), ("normal", normal, keep_normal)):
        for it in items:
            u = usable(it, od, judge_on)
            status[src][u] += 1
            if u == "ok":
                keep.append(it)
            elif u == "review":
                review.append(it)
    bank = []
    for it in test:
        u = usable(it, od, judge_on)
        status["test"][u] += 1
        if u == "ok":
            bank.append(it)
        elif u == "review":
            review.append(it)
    # 关键局面：每个模板内部平衡答案
    by_sc = defaultdict(list)
    for it in keep_key:
        by_sc[it["scenario"]].append(it)
    key_bal = []
    for sc, items in sorted(by_sc.items()):
        key_bal.extend(cap_tags(items, float(mc["max_tag_share"]), rng))
    norm_bal = cap_generals(keep_normal, float(mc["max_general_share"]), rng)
    # 配比
    s = float(mc["key_share"])
    K, N = len(key_bal), len(norm_bal)
    if K and N and 0 < s < 1:
        if K / (K + N) > s:
            key_bal = _even_sample(key_bal, max(1, round(s / (1 - s) * N)), rng)
        else:
            norm_bal = sample(norm_bal, max(1, round((1 - s) / s * K)), rng)
    train = key_bal + norm_bal
    rng.shuffle(train)
    n_sft = n_dpo = 0
    with open(os.path.join(top, "sft.jsonl"), "w", encoding="utf-8") as f:
        for it in train:
            msgs = it["messages"] + [{"role": "assistant", "content": answer(it, it["best"])}]
            f.write(json.dumps({"messages": msgs, "meta": meta(it)}, ensure_ascii=False) + "\n")
            n_sft += 1
    with open(os.path.join(top, "dpo.jsonl"), "w", encoding="utf-8") as f:
        for it in train:
            worse = sorted(it["worse"], key=lambda j: -it["diffs"][j])[:int(mc["dpo_pairs_per_item"])]
            for j in worse:
                f.write(json.dumps({"prompt": it["messages"],
                                    "chosen": [{"role": "assistant", "content": answer(it, it["best"])}],
                                    "rejected": [{"role": "assistant", "content": answer(it, j)}],
                                    "margin": it["diffs"][j], "meta": meta(it)}, ensure_ascii=False) + "\n")
                n_dpo += 1
    with open(os.path.join(top, "test.jsonl"), "w", encoding="utf-8") as f:
        for it in bank:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    write_review(os.path.join(top, "review.md"), bank, review, int(mc["review_limit"]))
    report = write_stats(os.path.join(top, "stats.md"), status, key_bal, norm_bal, bank, n_sft, n_dpo, review)
    return {"train": len(train), "sft": n_sft, "dpo": n_dpo, "test": len(bank), "review": len(review),
            "key": len(key_bal), "normal": len(norm_bal), "report": report}


def _even_sample(items, k, rng):
    """按模板均匀地抽 k 道（每个模板内部的答案比例尽量不变）。"""
    by = defaultdict(list)
    for it in items:
        by[it["scenario"]].append(it)
    per = max(1, k // max(1, len(by)))
    out = []
    for v in by.values():
        out.extend(sample(v, per, rng))
    return out[:k] if len(out) > k else out


# ---------------------------------------------------------------------- 给人看的
def _fmt_side(s):
    hand = "、".join(s["hand"]) or "无"
    eq = "、".join(s["equips"])
    j = "、".join(s["judges"])
    out = f"{s['general']} {s['hp']} 血，手牌：{hand}"
    if eq:
        out += f"；装备：{eq}"
    if j:
        out += f"；判定区：{j}"
    return out


def item_md(it: Dict[str, Any]) -> str:
    sm = it["summary"]
    lines = [f"### {it['id']}（{it.get('title') or it['kind']}）",
             f"- 你：{_fmt_side(sm['me'])}",
             f"- 对手：{_fmt_side(sm['opp'])}（这是上帝视角，模型看不到对手手牌）",
             f"- 第 {sm['turn']} 回合，牌堆 {sm['deck']} 张。要决定：{it['prompt_text']}",
             "", "| 选项 | 胜率 | 比最好的差 | 判断 |", "|---|---|---|---|"]
    for j, lb in enumerate(it["group_labels"]):
        mark = "最好" if j == it["best"] else ("明显更差" if j in it.get("worse", []) else "差不多")
        lines.append(f"| {lb} | {it['values'][j]:.0%} | {it['diffs'][j]:+.0%} ±{2 * it['se'][j]:.0%} | {mark} |")
    lines.append(f"\n每个选项模拟了 {it['n']} 个随机世界。")
    j = it.get("judge")
    if j and j.get("votes"):
        maj = it["group_labels"][j["majority"]]
        lines.append(f"裁判（{j['votes']} 票）：多数选「{maj}」，{j['agree_share']:.0%} 的票在可接受的选项里。")
        for r in j.get("reasons", [])[:2]:
            if r.get("reason"):
                lines.append(f"  - 选「{it['group_labels'][r['group']]}」：{r['reason']}")
    return "\n".join(lines)


def write_review(path, bank, review, limit):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 人工复核\n\n胜率是“这样选之后，双方都按模拟者的水平打到终局”的胜率。"
                "你觉得答案不对的题，记下编号：可以从题库里删掉，也说明模拟者在这类局面打得不对。\n\n")
        if review:
            f.write(f"## 裁判和模拟意见不一致（{len(review)} 道，没进训练）\n\n")
            for it in review[:limit]:
                f.write(item_md(it) + "\n\n")
        f.write(f"## 题库（{len(bank)} 道）\n\n")
        by = defaultdict(list)
        for it in bank:
            by[it["scenario"]].append(it)
        per = max(1, limit // max(1, len(by)))
        for sc, items in sorted(by.items()):
            f.write(f"## {items[0].get('title')}（{sc}）\n\n")
            for it in items[:per]:
                f.write(item_md(it) + "\n\n")


def write_stats(path, status, key_bal, norm_bal, bank, n_sft, n_dpo, review) -> str:
    names = {"ok": "能用", "tie": "差不多/没结论（扔掉）", "review": "待复核", "drop": "裁判不同意（扔掉）"}
    lines = ["# 数据统计", ""]
    for src, title in (("key", "关键局面（训练）"), ("test", "关键局面（题库）"), ("normal", "普通局面")):
        c = status[src]
        if not c:
            continue
        tot = sum(c.values())
        lines.append(f"- {title}：共 {tot} 道，" + "，".join(f"{names.get(k, k)} {v}" for k, v in c.most_common()))
    lines += ["", f"训练集：关键局面 {len(key_bal)} + 普通局面 {len(norm_bal)}"
              + (f"（关键占 {len(key_bal) / max(1, len(key_bal) + len(norm_bal)):.0%}）" if key_bal or norm_bal else "")
              + f"；sft.jsonl {n_sft} 行，dpo.jsonl {n_dpo} 行；题库 {len(bank)} 道；待复核 {len(review)} 道。", ""]
    by = defaultdict(Counter)
    for it in key_bal:
        by[(it["scenario"], it.get("title"))][it["tag"]] += 1
    if by:
        lines += ["## 关键局面：每个模板的答案分布（训练集）", "", "| 模板 | 题数 | 答案分布 |", "|---|---|---|"]
        for (sc, title), c in sorted(by.items()):
            lines.append(f"| {title}（{sc}） | {sum(c.values())} | " + "，".join(f"{k} {v}" for k, v in c.most_common()) + " |")
    bt = defaultdict(Counter)
    for it in bank:
        bt[(it["scenario"], it.get("title"))][it["tag"]] += 1
    if bt:
        lines += ["", "## 题库的答案分布", "", "| 模板 | 题数 | 答案分布 |", "|---|---|---|"]
        for (sc, title), c in sorted(bt.items()):
            lines.append(f"| {title}（{sc}） | {sum(c.values())} | " + "，".join(f"{k} {v}" for k, v in c.most_common()) + " |")
    gc = Counter(it["general"] for it in norm_bal)
    kc = Counter(it["kind"] for it in norm_bal)
    if gc:
        lines += ["", "## 普通局面", "", "按武将：" + "，".join(f"{k} {v}" for k, v in gc.most_common()),
                  "", "按决策类型：" + "，".join(f"{k} {v}" for k, v in kc.most_common())]
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text
