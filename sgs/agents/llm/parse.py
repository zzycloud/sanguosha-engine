"""从模型回复里解析出动作编号。

按顺序尝试：去掉 <think>…</think> → JSON 里的 action 字段 → “action/选择/答案: N” → 整个回复就是一个数字
→ 回复正好等于某个选项的文字。都失败就返回错误原因，交给 LLMAgent 重试。
"""
from __future__ import annotations

import json
import re
from typing import Optional, Sequence, Tuple

THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
JSON_OBJ = re.compile(r"\{[^{}]*\}", re.S)
KEYED = re.compile(r"(?:\"?action\"?|动作|选择|选项|答案|编号)\s*[:：=是为]?\s*\"?(-?\d+)", re.I)
BARE = re.compile(r"^\s*[\[(（]?\s*(-?\d+)\s*[\])）.。]?\s*$")


def parse_action(text: str, n: int, labels: Sequence[str] = ()) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """返回 (编号, 错误原因, 理由)。成功时错误原因为 None。"""
    if text is None:
        return None, "回复为空", None
    t = THINK.sub("", text).strip()
    if "<think>" in t.lower():          # 思考没写完就被截断了
        t = t[t.lower().rfind("</think>") + 8:] if "</think>" in t.lower() else ""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.I)
    if not t:
        return None, "回复为空（可能被 max_tokens 截断）", None
    found: Optional[int] = None
    reason: Optional[str] = None
    for m in reversed(JSON_OBJ.findall(t)):
        try:
            obj = json.loads(m)
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            v = obj["action"]
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                v = int(v.strip())
            if isinstance(v, bool) or not isinstance(v, int):
                return None, f"action 不是整数：{v!r}", None
            found = v
            r = obj.get("reason")
            reason = r if isinstance(r, str) else None
            break
    if found is None:
        m = KEYED.search(t)
        if m:
            found = int(m.group(1))
    if found is None:
        m = BARE.match(t)
        if m:
            found = int(m.group(1))
    if found is None and labels:
        hits = [i for i, lb in enumerate(labels) if lb and lb == t]
        if len(hits) == 1:
            found = hits[0]
    if found is None:
        return None, "没有找到动作编号", None
    if not 0 <= found < n:
        return None, f"编号 {found} 超出范围", reason
    return found, None, reason
