"""终端进度：每隔几秒一行，写到 stderr。"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Dict, Optional, TextIO

from sgs.progress import fmt_secs

STATUS = {"confident": "有结论", "tie": "差不多", "single": "没得选", "failed": "摆不出"}


class Progress:
    def __init__(self, title: str, total: int, stream: TextIO, every: float = 2.0):
        self.title, self.total, self.stream, self.every = title, max(1, total), stream, every
        self.n = 0
        self.t0 = time.perf_counter()
        self.last = 0.0
        self.counts: Counter = Counter()
        print(f"\n[{self.title}] 开始，共 {total} 个", file=stream, flush=True)

    def tick(self, item: Optional[Dict[str, Any]]) -> None:
        self.n += 1
        if item is not None:
            st = "failed" if item.get("failed") else item.get("status", "")
            if item.get("judge") is not None:
                st = {True: "裁判同意", False: "裁判不同意", None: "裁判没答"}[item["judge"].get("agree")]
            self.counts[st] += 1
        now = time.perf_counter()
        if now - self.last >= self.every or self.n >= self.total:
            self.last = now
            self._line()

    def _line(self) -> None:
        dt = time.perf_counter() - self.t0
        rate = self.n / dt if dt > 0 else 0.0
        left = (self.total - self.n) / rate if rate > 0 else 0.0
        parts = "，".join(f"{STATUS.get(k, k)} {v}" for k, v in self.counts.most_common())
        print(f"[{self.title}] {self.n}/{self.total}（{self.n * 100 // self.total}%） 用时 {fmt_secs(dt)}，"
              f"{rate:.2f} 个/秒，还要约 {fmt_secs(left)}｜{parts}", file=self.stream, flush=True)

    def done(self) -> None:
        if self.n < self.total:
            self._line()
