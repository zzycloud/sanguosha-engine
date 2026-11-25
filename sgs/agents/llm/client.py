"""对话模型客户端。LLMAgent 只依赖 ChatClient.chat()，换协议就是换一个客户端类。

内置 "openai"：OpenAI 兼容的 /chat/completions 接口。vLLM、SGLang、Ollama（/v1）、
LM Studio、llama.cpp server 以及大多数云厂商都提供这个接口，所以绝大多数情况用它即可。
别名 vllm / sglang / ollama 只是默认 base_url 不同。

接别的协议（例如自己的推理服务、非 OpenAI 格式的 API）：

    from sgs.agents.llm import ChatClient, ChatResult, register_client

    @register_client("myproto")
    class MyClient(ChatClient):
        def __init__(self, url, timeout=60, **kw): ...
        def chat(self, messages, **params) -> ChatResult:
            ...  # 发请求，返回 ChatResult(text=..., request=..., response=...)

然后在智能体配置里写 client=myproto。只用标准库，不依赖 openai 包。
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

CLIENTS: Dict[str, Callable[..., "ChatClient"]] = {}


def register_client(name: str):
    def deco(cls):
        CLIENTS[name] = cls
        return cls
    return deco


class ChatError(RuntimeError):
    """请求最终失败（超时、连接失败、多次 5xx 等）。"""

    def __init__(self, msg: str, request: Optional[dict] = None, response: Any = None):
        super().__init__(msg)
        self.request = request
        self.response = response


@dataclass
class ChatResult:
    text: str
    request: Dict[str, Any]                 # 实际发出的请求体（不含 api_key）
    response: Any                           # 服务端原始返回（JSON）
    latency: float = 0.0                    # 秒
    usage: Optional[Dict[str, Any]] = None  # token 用量（服务端给了才有）
    http_attempts: int = 1
    reasoning: Optional[str] = None         # 有些服务把思考过程放在 reasoning_content 里
    extra: Dict[str, Any] = field(default_factory=dict)


class ChatClient:
    def chat(self, messages: List[Dict[str, str]], **params) -> ChatResult:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "vllm": "http://localhost:8000/v1",
    "sglang": "http://localhost:30000/v1",
    "ollama": "http://localhost:11434/v1",
}


class OpenAIChatClient(ChatClient):
    """POST {base_url}/chat/completions。

    api_key 取值顺序：api_key 参数 → api_key_env 指定的环境变量（默认 OPENAI_API_KEY）→ 不带鉴权头。
    遇到 408/409/429/5xx 和网络错误按指数退避重试 http_retries 次；4xx 其它错误直接失败。
    extra_body 原样合并进请求体，用来传服务端特有参数（例如 vLLM 的 guided_json、top_k）。
    """

    default_url = DEFAULT_URLS["openai"]

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 api_key_env: str = "OPENAI_API_KEY", timeout: float = 60.0, http_retries: int = 2,
                 backoff: float = 1.0, headers: Optional[Dict[str, str]] = None,
                 extra_body: Optional[Dict[str, Any]] = None, path: str = "/chat/completions"):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env or "", "") or None
        self.timeout = timeout
        self.http_retries = http_retries
        self.backoff = backoff
        self.headers = dict(headers or {})
        self.extra_body = dict(extra_body or {})
        self.path = path

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdr = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            hdr["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + self.path, data=data, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def chat(self, messages, **params) -> ChatResult:
        body = {"messages": messages, **{k: v for k, v in params.items() if v is not None}, **self.extra_body}
        t0 = time.perf_counter()
        last: Any = None
        for attempt in range(self.http_retries + 1):
            try:
                resp = self._post(body)
                break
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:2000]
                except Exception:
                    detail = ""
                last = f"HTTP {e.code}: {detail}"
                if e.code not in (408, 409, 429) and e.code < 500:
                    raise ChatError(last, body, detail) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            except ValueError as e:  # 返回的不是 JSON
                last = f"返回无法解析：{e}"
            if attempt < self.http_retries:
                time.sleep(self.backoff * (2 ** attempt) * (0.5 + random.random()))
        else:
            raise ChatError(f"请求失败（{self.http_retries + 1} 次）：{last}", body, last)
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatError(f"返回里没有 choices[0].message：{str(resp)[:500]}", body, resp) from None
        return ChatResult(text=msg.get("content") or "", request=body, response=resp,
                          latency=time.perf_counter() - t0, usage=resp.get("usage"),
                          http_attempts=attempt + 1,
                          reasoning=msg.get("reasoning_content") or msg.get("reasoning"))


register_client("openai")(OpenAIChatClient)
for _name in ("vllm", "sglang", "ollama"):
    register_client(_name)(type(f"{_name.title()}Client", (OpenAIChatClient,), {"default_url": DEFAULT_URLS[_name]}))


def make_client(name: str = "openai", **kw) -> ChatClient:
    if name in CLIENTS:
        return CLIENTS[name](**kw)
    if ":" in name:
        import importlib
        mod, _, attr = name.partition(":")
        return getattr(importlib.import_module(mod), attr)(**kw)
    raise KeyError(f"未知的客户端 {name!r}；已注册：{', '.join(sorted(CLIENTS))}，或写成 '模块:类名'")
