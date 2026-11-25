"""对战时终端里的进度：开局/结束各一行、定时心跳、请求失败和超时立即一行。

级别（output.progress）：
  quiet    只打印开头的配置摘要、出错、最后的汇总
  normal   默认：再加每局开始/结束、每 heartbeat 秒一行心跳、请求失败和超时
  verbose  再加每个模型决策一行（选了什么、用时、来源）

全部写到 stderr，并发对局共用一个 Progress，线程安全。
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, TextIO

LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}


def level_of(v: Any) -> int:
    if v is True or v is None:
        return 1
    if v is False:
        return 0
    s = str(v).lower()
    if s in LEVELS:
        return LEVELS[s]
    raise ValueError(f"output.progress 只能是 quiet / normal / verbose，不是 {v!r}")


def fmt_secs(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s} 秒"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {s:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m:02d} 分"


class GameState:
    """一局进行中的状态（心跳用）。"""

    def __init__(self, k: int, generals: Sequence[str], seats: Sequence[int], names: Sequence[str]):
        self.k = k
        self.generals = list(generals)
        self.seats = list(seats)
        self.names = list(names)
        self.t0 = time.perf_counter()
        self.turn = 0
        self.decisions = 0
        self.llm_decisions = 0
        self.latency = 0.0
        self.illegal = 0
        self.fallbacks = 0
        self.failed = 0
        self.waiting: Optional[tuple] = None     # (座位, 开始等的时间)


class Progress:
    def __init__(self, level: Any = "normal", heartbeat: Optional[float] = 30.0, stream: Optional[TextIO] = None,
                 total: int = 0, names: Sequence[str] = ("A", "B")):
        self.level = level_of(level)
        self.heartbeat = heartbeat if heartbeat and heartbeat > 0 else None
        self.stream = stream
        self.total = total
        self.names = list(names)
        self.t0 = time.perf_counter()
        self.lock = threading.Lock()
        self.running: Dict[int, GameState] = {}
        self.done = 0
        self.wins = [0, 0]
        self.other = 0
        self.stopping = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- 输出
    def say(self, msg: str, level: int = 1) -> None:
        if self.level >= level:
            with self.lock:
                print(msg, file=self.stream or sys.stderr, flush=True)

    def _score(self) -> str:
        return f"A {self.wins[0]} : {self.wins[1]} B" + (f"，平局或中止 {self.other}" if self.other else "")

    def _elapsed(self) -> str:
        return fmt_secs(time.perf_counter() - self.t0)

    # ------------------------------------------------------------- 心跳
    def start(self) -> None:
        if self.heartbeat and self.level >= 1:
            self._thread = threading.Thread(target=self._beat, name="sgs-heartbeat", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat):
            self.say(self.heartbeat_line())

    def heartbeat_line(self) -> str:
        with self.lock:
            games = list(self.running.values())
            head = f"[进度 {time.strftime('%H:%M:%S')}，已用 {self._elapsed()}] 完成 {self.done}/{self.total}（{self._score()}）"
        if not games:
            return head + "；没有进行中的局"
        parts = []
        now = time.perf_counter()
        for g in sorted(games, key=lambda x: x.k):
            s = f"第 {g.k} 局 {'/'.join(g.generals)} 第 {g.turn} 回合，{g.decisions} 个决策"
            if g.llm_decisions:
                s += f"，模型 {g.llm_decisions} 次 平均 {g.latency / g.llm_decisions:.1f} 秒"
            bad = [f"答错 {g.illegal}"] if g.illegal else []
            bad += [f"兜底 {g.fallbacks}"] if g.fallbacks else []
            bad += [f"请求失败 {g.failed}"] if g.failed else []
            if bad:
                s += "，" + "、".join(bad)
            if g.waiting:
                seat, t = g.waiting
                s += f"，正在等 {g.generals[seat]} 的模型回复" + (f"（已等 {fmt_secs(now - t)}）" if now - t >= 5 else "")
            parts.append(s)
        return head + "\n    " + "\n    ".join(parts)

    # ------------------------------------------------------------- 事件
    def game_start(self, g: GameState) -> None:
        with self.lock:
            self.running[g.k] = g
        who = "，".join(f"{g.generals[s]}（{s} 号位，{g.names[s]}）" for s in range(len(g.generals)))
        self.say(f"[开始] 第 {g.k}/{self.total} 局：{who}")

    def game_end(self, info: Dict[str, Any]) -> None:
        with self.lock:
            g = self.running.pop(info["game"], None)
            self.done += 1
            if info.get("winner_side") is None:
                self.other += 1
            else:
                self.wins[info["winner_side"]] += 1
            score = self._score()
            elapsed = self._elapsed()
        w = "AB"[info["winner_side"]] if info.get("winner_side") is not None else "-"
        msg = (f"[结束] 第 {info['game']}/{self.total} 局 {'/'.join(info['generals'])}：{info['result']}，胜方 {w}，"
               f"{info.get('turns')} 回合，{info.get('seconds')} 秒")
        if info.get("llm_calls"):
            msg += f"；模型决策 {info['llm_calls']}（请求 {info.get('llm_requests', 0)} 次），答错 {info.get('illegal', 0)}，兜底 {info.get('fallbacks', 0)}"
        if info.get("error_kinds"):
            msg += "，请求失败 " + "、".join(f"{k}×{v}" for k, v in info["error_kinds"].items())
        if info.get("error"):
            msg += f"（出错：{info['error']}）"
        msg += f"。总比分 {score}，完成 {self.done}/{self.total}，已用 {elapsed}"
        self.say(msg, 0 if info.get("error") else 1)

    def decision(self, g: GameState, seat: int, label: str, info: Optional[Dict[str, Any]], calls: List[Dict[str, Any]]):
        """一个需要选择的决策做完了。"""
        with self.lock:
            g.decisions += 1
            g.waiting = None
            for c in calls:
                g.llm_decisions += 1
                g.latency += (info or {}).get("latency") or 0
                for a in c.get("attempts", []):
                    if a.get("error_kind") == "illegal":
                        g.illegal += 1
                if c.get("source") == "fallback":
                    g.fallbacks += 1
        if calls and self.level >= 2:
            i = info or {}
            src = {"llm": "模型", "fallback": "兜底", "single": "唯一选项", "auto": "自动"}.get(i.get("source"), i.get("source"))
            self.say(f"    第 {g.k} 局 第 {g.turn} 回合 {g.generals[seat]}：{label}（{src}，{i.get('latency')} 秒"
                     + (f"，请求 {i['attempts']} 次" if i.get("attempts", 1) > 1 else "") + "）", 2)
        for c in calls:
            if c.get("source") == "fallback":
                errs = [a.get("error") for a in c.get("attempts", []) if a.get("error")]
                self.say(f"[兜底] 第 {g.k} 局 第 {g.turn} 回合 {g.generals[seat]}：模型没给出合法动作，用兜底 AI 选了“{label}”"
                         + (f"；最后的错误：{errs[-1]}" if errs else ""))

    def waiting_for(self, g: GameState, seat: int) -> None:
        with self.lock:
            g.waiting = (seat, time.perf_counter())

    def http_event(self, g: GameState, seat: int, ev: Dict[str, Any]) -> None:
        """客户端每次 HTTP 尝试失败时调（会重试的也调），立即打印。"""
        with self.lock:
            g.failed += 1
        what = f"HTTP {ev['status']}" if ev.get("status") else (ev.get("error") or "失败")
        body = ev.get("body")
        if ev.get("status"):
            what += f"，响应体{'为空' if not body else '：' + body[:120]}"
        tail = f"，{ev['wait']:.1f} 秒后重试（第 {ev['attempt'] + 1}/{ev['max']} 次）" if ev.get("wait") is not None else "，不再重试，交给兜底"
        px = f"，经过代理 {ev['proxy']}" if ev.get("proxy") else ""
        self.say(f"[请求失败] 第 {g.k} 局 {g.generals[seat]}（{g.names[seat]}）：{what}{px}{tail}")

    def note(self, msg: str, level: int = 1) -> None:
        self.say(msg, level)
