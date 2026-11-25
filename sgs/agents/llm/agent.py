"""LLMAgent：用对话模型打牌。

每个决策点：渲染提示词 → 调模型 → 解析编号；输出不合法就把错误告诉模型再问（最多 max_retries 次）；
网络失败、超时或一直不合法，就交给兜底智能体（默认规则 AI），保证对局不会卡住。
每次决策的完整请求和回复（含重试）写进 transcript，可直接拿来做 SFT/偏好数据。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

from ..base import Agent
from .client import ChatClient, ChatError, make_client
from .parse import parse_action
from .prompt import PromptBuilder, make_prompt

TRANSCRIPT_FORMAT = "sgs-llm-call/1"


class JsonlSink:
    """线程安全地往 JSONL 文件追加记录（并发对局共用一个）。"""

    def __init__(self, path: str, mode: str = "a"):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._f = open(path, mode, encoding="utf-8")

    def __call__(self, rec: Dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self) -> None:
        with self._lock:
            self._f.close()


class LLMAgent(Agent):
    """配置项（命令行里写成 llm?model=...&base_url=...，或放进 JSON 文件）：

    连接：client（openai/vllm/sglang/ollama 或 '模块:类名'）、base_url、model、api_key、
          api_key_env（默认 OPENAI_API_KEY）、timeout（单次请求秒数）、http_retries、backoff、headers、extra_body
    采样：temperature、top_p、max_tokens、stop、sample_seed（发给服务端的采样种子）；seed 只给兜底 AI 用
    提示词：prompt（模板名）、history_lines、ask_reason、system_prompt、extra_rules、show_skill_text
    容错：max_retries（输出不合法时重问几次）、decision_timeout（一次决策总共最多等几秒）、
          fallback（兜底智能体，默认 rule；写 none 表示不兜底、直接报错）
    其它：skip_single（只有一个选项时不调模型，默认开）、transcript（JSONL 路径）、label（显示名）
    """

    name = "llm"
    needs_history = True
    needs_derived = True

    def __init__(self, model: str = "", base_url: Optional[str] = None, api_key: Optional[str] = None,
                 client: Union[str, ChatClient] = "openai", api_key_env: str = "OPENAI_API_KEY",
                 timeout: float = 60.0, http_retries: int = 2, backoff: float = 1.0, headers: Optional[Dict[str, str]] = None,
                 extra_body: Optional[Dict[str, Any]] = None,
                 temperature: Optional[float] = 0.7, top_p: Optional[float] = None,
                 max_tokens: Optional[int] = None, stop: Any = None, seed: Optional[int] = None,
                 sample_seed: Optional[int] = None,
                 prompt: Union[str, PromptBuilder] = "default", history_lines: int = 40,
                 ask_reason: bool = False, system_prompt: Optional[str] = None, extra_rules: str = "",
                 show_skill_text: bool = True,
                 max_retries: int = 2, decision_timeout: Optional[float] = None,
                 fallback: Optional[Any] = "rule", skip_single: bool = True,
                 transcript: Union[None, str, Callable[[Dict[str, Any]], None]] = None,
                 label: Optional[str] = None, **unknown):
        if unknown:
            raise TypeError(f"LLMAgent 不认识这些配置：{', '.join(unknown)}")
        self.model = model
        self.client = client if isinstance(client, ChatClient) else make_client(
            client, base_url=base_url, api_key=api_key, api_key_env=api_key_env, timeout=timeout,
            http_retries=http_retries, backoff=backoff, headers=headers, extra_body=extra_body)
        self.params = {"model": model, "temperature": temperature, "top_p": top_p,
                       "max_tokens": max_tokens, "stop": stop, "seed": sample_seed}
        self.prompt = prompt if isinstance(prompt, PromptBuilder) else make_prompt(
            prompt, history_lines=history_lines, ask_reason=ask_reason, system_prompt=system_prompt,
            extra_rules=extra_rules, show_skill_text=show_skill_text)
        self.max_retries = max_retries
        self.decision_timeout = decision_timeout
        self.skip_single = skip_single
        self.label = label or (f"llm:{model}" if model else "llm")
        self.fallback: Optional[Agent] = None
        if fallback not in (None, "none", ""):
            from ..registry import make_agent
            self.fallback = make_agent(fallback, seed=seed)
        self._own_sink = isinstance(transcript, str)
        self.sink = JsonlSink(transcript) if isinstance(transcript, str) else transcript
        self.game: Dict[str, Any] = {}
        self.stats = {"decisions": 0, "llm_calls": 0, "illegal": 0, "errors": 0, "fallbacks": 0,
                      "latency": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    # ------------------------------------------------------------- 生命周期
    def on_game_start(self, info):
        self.game = dict(info)
        if self.fallback is not None:
            self.fallback.on_game_start(info)

    def close(self):
        if self._own_sink and self.sink is not None:
            self.sink.close()
        self.client.close()

    # ------------------------------------------------------------- 决策
    def choose(self, view, d) -> int:
        n = len(d.options)
        if n == 1 and self.skip_single:
            self.last_info = {"source": "single"}
            return 0
        self.stats["decisions"] += 1
        labels = [o.label for o in d.options]
        messages = self.prompt.messages(view, d)
        attempts: List[Dict[str, Any]] = []
        t0 = time.perf_counter()
        action = reason = None
        source = "fallback"
        for k in range(self.max_retries + 1):
            if self.decision_timeout and time.perf_counter() - t0 > self.decision_timeout:
                attempts.append({"error": f"本次决策已用时超过 {self.decision_timeout} 秒"})
                break
            rec: Dict[str, Any] = {"messages": list(messages)}
            self.stats["llm_calls"] += 1
            try:
                r = self.client.chat(messages, **self.params)
            except ChatError as e:
                self.stats["errors"] += 1
                rec.update(error=str(e), response=e.response)
                attempts.append(rec)
                break          # 客户端已经按 http_retries 重试过，再问也没用，直接兜底
            self.stats["latency"] += r.latency
            for key in ("prompt_tokens", "completion_tokens"):
                self.stats[key] += int((r.usage or {}).get(key) or 0)
            i, err, why = parse_action(r.text, n, labels)
            rec.update(request={k2: v for k2, v in r.request.items() if k2 != "messages"},
                       response=r.response, text=r.text, reasoning=r.reasoning, latency=round(r.latency, 4),
                       usage=r.usage, http_attempts=r.http_attempts, parsed=i, error=err)
            attempts.append(rec)
            if err is None:
                action, reason, source = i, why, "llm"
                break
            self.stats["illegal"] += 1
            messages = messages + [{"role": "assistant", "content": r.text or ""}, self.prompt.retry(err, n)]
        if action is None:
            self.stats["fallbacks"] += 1
            if self.fallback is None:
                last = attempts[-1].get("error") if attempts else "未知原因"
                raise RuntimeError(f"{self.label} 没能给出合法动作：{last}")
            action = self.fallback.choose(view, d)
        self.last_info = {"source": source, "attempts": len(attempts), "reason": reason,
                          "latency": round(time.perf_counter() - t0, 4),
                          "errors": [a["error"] for a in attempts if a.get("error")]}
        if self.sink is not None:
            self.sink({
                "format": TRANSCRIPT_FORMAT, "game_id": self.game.get("game_id"), "seat": d.player,
                "agent": self.label, "model": self.model, "step": getattr(view, "step", None),
                "turn": view.turn, "phase": view.phase, "kind": d.kind, "prompt": d.prompt,
                "legal_actions": labels, "attempts": attempts, "action": action,
                "action_label": labels[action], "source": source, "reason": reason,
            })
        return action
