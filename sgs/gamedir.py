"""对战输出里“一局一个目录”的部分：每局的录像、轨迹、模型调用放在一起，按步数对齐；顶层一个总索引页。

    runs/xxx/
      index.html            总索引：每局胜负、武将、模型调用/答错/兜底次数，点进去看录像和轨迹
      summary.json          汇总统计
      games.json            每局一条汇总（缩进 JSON）
      games/0001/
        replay.html         录像；每个决策点下面直接展开模型的完整提示词和原始回复
        game.md             易读的轨迹：发生了什么、每个决策点的问题/选项/选择/来源，模型调用原文
        trajectory.json     每个决策点：决策者视角的观察、合法动作、选择、来源（缩进 JSON）
        llm_calls.json      这局全部模型调用：完整提示词、原始回复、解析结果、重试、token、耗时
        result.json         这局汇总

对齐用的编号：
  replay_step   录像里的第几步（录像页面右下角显示“第 N 步”，地址后面加 #step=N 可以直接跳过去）
  step          引擎里这局第几个决策（含只有一个选项、自动选掉的）
  call          这局第几次模型决策调用（llm_calls.json 的下标；一次决策可能含多次重试，都在 attempts 里）
"""
from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from .core.game import log_text

GAMES_DIR = "games"


def game_dirname(k: int) -> str:
    """第 k 局（从 1 开始）的目录名。"""
    return f"{k:04d}"


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------- Markdown 轨迹

SOURCE_TEXT = {"llm": "模型", "fallback": "兜底（模型没给出合法动作）", "single": "唯一选项", "auto": "不用想（自动）"}
PHASE_TEXT = {"prepare": "准备阶段", "judge": "判定阶段", "draw": "摸牌阶段", "play": "出牌阶段",
              "discard": "弃牌阶段", "finish": "结束阶段", None: ""}
KIND_TEXT = {"play": "出牌", "respond": "响应", "trigger": "发动", "choose": "选择"}


def _fence(text: str, lang: str = "text") -> str:
    text = "" if text is None else str(text)
    tick = "```"
    while tick in text:
        tick += "`"
    return f"{tick}{lang}\n{text}\n{tick}"


def _call_md(call: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for n, a in enumerate(call.get("attempts", []), 1):
        bits = []
        if a.get("latency") is not None:
            bits.append(f"{a['latency']} 秒")
        u = a.get("usage") or {}
        if u.get("prompt_tokens") is not None:
            bits.append(f"提示 {u.get('prompt_tokens')} token，回复 {u.get('completion_tokens')} token")
        if a.get("error"):
            bits.append(f"**不合法：{a['error']}**")
        elif a.get("parsed") is not None:
            bits.append(f"解析为 {a['parsed']}")
        out.append(f"<details><summary>第 {n} 次请求（{'，'.join(bits) or '没有回复'}）</summary>\n")
        msgs = a.get("messages") or []
        # 重试时前面的消息和第一次一样，只放新增的部分
        prev = (call.get("attempts") or [{}])[0].get("messages") or [] if n > 1 else []
        start = len(prev) if n > 1 and msgs[:len(prev)] == prev else 0
        if start:
            out.append(f"（前 {start} 条消息同第 1 次请求）\n")
        for m in msgs[start:]:
            out.append(f"**{m.get('role')}**\n")
            out.append(_fence(m.get("content")) + "\n")
        if a.get("reasoning"):
            out.append("**模型的思考**\n")
            out.append(_fence(a["reasoning"]) + "\n")
        out.append("**模型的原始回复**\n")
        out.append(_fence(a.get("text") if a.get("text") is not None else a.get("response")) + "\n")
        out.append("</details>\n")
    return out


class GameMarkdown:
    """边打边写的 Markdown 轨迹：开局写标题，每个决策点追加一节（含之前发生的事），结束时追加结果。

    out 是一个可写的文本文件（每次追加后 flush，中途中止也能看到已经打过的部分）。"""

    def __init__(self, out, info: Dict[str, Any]):
        self.out = out
        self.li = 0
        self.pending: List[str] = []
        self.turn = None
        gens, sides = info["generals"], info["agents"]
        self._w([f"# 第 {info['game']} 局：{gens[0]}（0 号位，{sides[0]}）对 {gens[1]}（1 号位，{sides[1]}）", "",
                 f"- 种子 {info['seed']}；录像：[replay.html](replay.html)（一局结束后生成）；"
                 f"结构化数据：trajectory.json、llm_calls.json",
                 "- 下面每个决策点标了“录像第 N 步”，在录像页面地址后面加 `#step=N` 可以直接跳到那一步。", ""])

    def _w(self, lines: List[str]) -> None:
        self.out.write("\n".join(lines) + "\n")
        self.out.flush()

    def _events(self, logs, upto: Optional[int] = None) -> List[str]:
        upto = len(logs) if upto is None else upto
        evs = self.pending + [t for t in (log_text(e, None) for e in logs[self.li:upto]) if t]
        self.pending = []
        self.li = max(self.li, upto)
        return evs

    def auto(self, r: Dict[str, Any], logs: Sequence[Dict[str, Any]]) -> None:
        """一个没交给智能体、直接选掉的决策：记在下一个决策点的“发生了”里。"""
        self.pending = self._events(logs, r.get("log_index", self.li))
        self.pending.append(f"（自动，{r['auto']}）{r['general']}：{r['decision']['prompt']} → {r['action_label']}")

    def decision(self, r: Dict[str, Any], logs: Sequence[Dict[str, Any]], calls: Sequence[Dict[str, Any]]) -> None:
        lines: List[str] = []
        evs = self._events(logs, r.get("log_index", self.li))
        if r["turn"] != self.turn:
            self.turn = r["turn"]
            lines += [f"## 第 {self.turn} 回合", ""]
        if evs:
            lines += ["发生了：", ""] + [f"- {t}" for t in evs] + [""]
        ai = r.get("agent_info") or {}
        lines += [(f"### 录像第 {r.get('replay_step')} 步 · {r['general']}（{r['player']} 号位）· "
                   f"{PHASE_TEXT.get(r.get('phase'), r.get('phase') or '')} · "
                   f"{KIND_TEXT.get(r['decision']['kind'], r['decision']['kind'])}"), "",
                  f"问题：{r['decision']['prompt']}", ""]
        for i, a in enumerate(r["legal_actions"]):
            lines.append(f"{i}. {a['label']}" + ("  ← **选择**" if i == r["action"] else ""))
        lines.append("")
        src, who = ai.get("source"), r.get("agent") or ""
        if src:
            s = f"来源：{who}，{SOURCE_TEXT.get(src, src)}"
            if ai.get("attempts"):
                s += f"，请求 {ai['attempts']} 次"
            if ai.get("latency") is not None:
                s += f"，用时 {ai['latency']} 秒"
            lines.append(s)
            if ai.get("errors"):
                lines.append(f"答错或失败：{'；'.join(ai['errors'])}")
            if ai.get("reason"):
                lines.append(f"模型给的理由：{ai['reason']}")
        else:
            lines.append(f"来源：{who}")
        lines.append("")
        for c in calls:
            lines += _call_md(c)
        self._w(lines)

    def finish(self, info: Dict[str, Any], logs: Sequence[Dict[str, Any]]) -> None:
        lines: List[str] = []
        rest = self._events(logs)
        if rest:
            lines += ["## 最后", "", "发生了：", ""] + [f"- {t}" for t in rest] + [""]
        lines += ["## 结果", "", f"- {info.get('result')}",
                  f"- {info.get('turns')} 回合，{info.get('decisions')} 个需要选择的决策，"
                  f"交给模型 {info.get('llm_calls', 0)} 次（请求 {info.get('llm_requests', 0)} 次），"
                  f"答错 {info.get('illegal', 0)} 次，兜底 {info.get('fallbacks', 0)} 次，用时 {info.get('seconds')} 秒"]
        if info.get("error"):
            lines.append(f"- 出错：{info['error']}")
        self._w(lines + [""])


def game_markdown(info: Dict[str, Any], logs: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]],
                  calls: Sequence[Dict[str, Any]]) -> str:
    """一次性把一局写成 Markdown（和 GameMarkdown 边打边写的结果一样）。"""
    import io
    buf = io.StringIO()
    by_call = {c["call"]: c for c in calls if "call" in c}
    md = GameMarkdown(buf, info)
    for r in rows:
        md.decision(r, logs, [by_call[c] for c in r.get("llm_calls") or [] if c in by_call])
    md.finish(info, logs)
    return buf.getvalue()


class Throttled:
    """把整份 JSON 重写到文件，但最多每 interval 秒一次（先写临时文件再改名，文件随时都是完整的 JSON）。"""

    def __init__(self, path: str, interval: float = 2.0):
        self.path = path
        self.interval = interval
        self.last = 0.0

    def write(self, data_fn, force: bool = False) -> None:
        import time
        now = time.perf_counter()
        if not force and now - self.last < self.interval:
            return
        self.last = now
        tmp = self.path + ".tmp"
        write_json(tmp, data_fn())
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------- 总索引页

INDEX_CSS = """
:root { --bg:#efe9dd; --panel:#fbf8f1; --panel-2:#f4efe4; --ink:#221e18; --muted:#857b6c; --line:#d9cfbd;
  --accent:#8f2a1c; --red:#b8321f; --green:#3f7d4e; --amber:#a8741a;
  --sans:"PingFang SC","Noto Sans SC","Microsoft YaHei",system-ui,sans-serif; --serif:"Songti SC","Noto Serif SC",serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#141215; --panel:#1f1c21; --panel-2:#262229; --ink:#efe8dc;
  --muted:#978d7f; --line:#36303a; --accent:#e0745c; --red:#ff7a64; --green:#7cc28c; --amber:#e7b04c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 var(--sans); }
main { max-width: 1280px; margin: 0 auto; padding: 20px 16px 40px; }
h1 { font-family: var(--serif); font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin-bottom: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.card b { font-family: var(--serif); font-size: 16px; }
.card .big { font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; }
.card .row { color: var(--muted); font-size: 13px; }
.wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
th { background: var(--panel-2); font-weight: 700; position: sticky; top: 0; cursor: pointer; user-select: none; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
tr:hover td { background: var(--panel-2); }
.win { color: var(--green); font-weight: 700; }
.bad { color: var(--red); font-weight: 700; }
.warn { color: var(--amber); font-weight: 700; }
.muted { color: var(--muted); }
a { color: var(--accent); }
.filters { display: flex; gap: 12px; align-items: center; margin: 0 0 10px; color: var(--muted); font-size: 13px; flex-wrap: wrap; }
"""


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def index_html(summary: Dict[str, Any], games: Sequence[Dict[str, Any]], title: str = "对战结果") -> str:
    names = {k: v["agent"] for k, v in summary.get("sides", {}).items()}
    cards = []
    for side, v in summary.get("sides", {}).items():
        rows = [f"<div class='row'>先手 {v['wins_first']}/{v['games_first']} 胜</div>"]
        L = v.get("llm")
        if L:
            rows.append(f"<div class='row'>模型决策 {L.get('decisions', 0)} 次，请求 {L.get('llm_calls', 0)} 次，"
                        f"答错 {L.get('illegal', 0)}，兜底 {L.get('fallbacks', 0)}，平均 {L.get('avg_latency')} 秒</div>")
            if L.get("error_kinds"):
                rows.append("<div class='row bad'>请求失败：" + _esc("、".join(f"{k}×{v}" for k, v in L["error_kinds"].items())) + "</div>")
        cards.append(f"<div class='card'><b>{side} · {_esc(v['agent'])}</b>"
                     f"<div class='big'>{v['wins']}/{summary['games']}</div>{''.join(rows)}</div>")
    body = []
    for r in games:
        d = r.get("dir")
        side_of = {r["seats"][s]: "AB"[s] for s in range(2)}
        seat_cells = []
        for seat in range(2):
            side = side_of.get(seat, "?")
            cls = "win" if r.get("winner_seat") == seat else ""
            seat_cells.append(f"<td class='{cls}'>{_esc(r['generals'][seat])} <span class='muted'>({side})</span></td>")
        win = "AB"[r["winner_side"]] if r.get("winner_side") is not None else "—"
        ill, fb = r.get("illegal", 0), r.get("fallbacks", 0)
        links = []
        if d and r.get("replay"):
            links.append(f"<a href='{_esc(d)}/replay.html'>录像</a>")
        if d:
            links.append(f"<a href='{_esc(d)}/game.md'>轨迹</a>")
            links.append(f"<a href='{_esc(d)}/trajectory.json'>json</a>")
            if r.get("llm_calls"):
                links.append(f"<a href='{_esc(d)}/llm_calls.json'>模型调用</a>")
        err = f"<span class='bad'>{_esc(r['error'])}</span>" if r.get("error") else ""
        if r.get("error_kinds"):
            err += " <span class='bad'>请求失败 " + _esc("、".join(f"{k}×{v}" for k, v in r["error_kinds"].items())) + "</span>"
        body.append(
            f"<tr data-bad='{1 if (ill or fb or r.get('error')) else 0}'><td class='n'>{r['game']}</td><td class='n'>{r['seed']}</td>"
            + "".join(seat_cells)
            + f"<td>{win}</td><td>{_esc(r.get('result'))} {err}</td><td class='n'>{r.get('turns')}</td>"
            f"<td class='n'>{r.get('decisions')}</td><td class='n'>{r.get('llm_calls', 0)}</td><td class='n'>{r.get('llm_requests', 0)}</td>"
            f"<td class='n {'warn' if ill else ''}'>{ill}</td><td class='n {'bad' if fb else ''}'>{fb}</td>"
            f"<td class='n'>{r.get('seconds')}</td><td>{' · '.join(links)}</td></tr>")
    sub = (f"{summary['games']} 局，平局或中止 {summary.get('draws_or_aborted', 0)}，出错 {summary.get('errors', 0)}"
           + (f"，用时 {summary['seconds']} 秒" if summary.get("seconds") is not None else ""))
    vs = " 对 ".join(_esc(names.get(s, s)) for s in ("A", "B"))
    script = """
document.querySelectorAll('th').forEach((th, c) => th.onclick = () => {
  const tb = document.querySelector('tbody'), rows = [...tb.rows], asc = th.dataset.asc !== '1';
  th.dataset.asc = asc ? '1' : '0';
  const key = (r) => { const t = r.cells[c].innerText.trim(), n = parseFloat(t); return isNaN(n) ? t : n; };
  rows.sort((a, b) => { const x = key(a), y = key(b); return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); });
  rows.forEach((r) => tb.appendChild(r));
});
document.getElementById('onlybad').onchange = (e) => document.querySelectorAll('tbody tr').forEach((r) =>
  r.hidden = e.target.checked && r.dataset.bad !== '1');
"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title><style>{INDEX_CSS}</style></head>
<body><main>
<h1>{vs}</h1><div class="sub">{_esc(sub)}</div>
<div class="cards">{''.join(cards)}</div>
<div class="filters"><label><input type="checkbox" id="onlybad"> 只看有答错、兜底或出错的局</label>
<span>点表头排序；“轨迹”是每局的 Markdown（提示词和回复原文都在里面），“录像”里每个决策点下面能展开模型的输入输出。</span></div>
<div class="wrap"><table>
<thead><tr><th>局</th><th>种子</th><th>0 号位（先手）</th><th>1 号位</th><th>胜方</th><th>结果</th><th>回合</th><th>决策</th>
<th>模型决策</th><th>请求</th><th>答错</th><th>兜底</th><th>秒</th><th>查看</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
</main><script>{script}</script></body></html>
"""
