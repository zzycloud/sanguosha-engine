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

import email.utils
import ipaddress
import json
import os
import random
import socket
import time
import urllib.error
import urllib.parse
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
    """请求最终失败（超时、连接失败、多次 5xx 等）。

    kind 是错误类型（汇总统计用，例如 "HTTP 503"、"timeout"、"connect"），
    diagnostics 是排查用的细节：实际请求的地址、是否走了代理、每次 HTTP 尝试的状态码、响应头、响应体、等待时间。
    """

    def __init__(self, msg: str, request: Optional[dict] = None, response: Any = None,
                 kind: str = "error", diagnostics: Optional[Dict[str, Any]] = None):
        super().__init__(msg)
        self.request = request
        self.response = response
        self.kind = kind
        self.diagnostics = diagnostics or {}


@dataclass
class ChatResult:
    text: str
    request: Dict[str, Any]                 # 实际发出的请求体（不含 api_key）
    response: Any                           # 服务端原始返回（JSON）
    latency: float = 0.0                    # 秒
    usage: Optional[Dict[str, Any]] = None  # token 用量（服务端给了才有）
    http_attempts: int = 1
    reasoning: Optional[str] = None         # 有些服务把思考过程放在 reasoning_content 里
    extra: Dict[str, Any] = field(default_factory=dict)   # diagnostics：重试过时每次尝试的细节


class ChatClient:
    observer: Optional[Callable[[Dict[str, Any]], None]] = None   # 每次 HTTP 尝试失败时回调（进度显示用）

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

RETRY_STATUS = {408, 409, 425, 429}
KEEP_HEADERS = ("server", "via", "retry-after", "content-type", "content-length", "date", "x-request-id",
                "x-proxy-error", "proxy-status", "x-cache", "x-envoy-upstream-service-time", "cf-ray")


def is_local_host(host: str) -> bool:
    """本机或内网地址：localhost、127.x、::1、10.x、192.168.x、172.16-31.x、*.local、0.0.0.0。"""
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "0.0.0.0", "::1") or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def system_proxy_for(url: str) -> Optional[str]:
    """按环境变量（HTTP_PROXY/HTTPS_PROXY/NO_PROXY，macOS/Windows 上还有系统设置）这个地址会走哪个代理。"""
    parts = urllib.parse.urlsplit(url)
    proxies = urllib.request.getproxies()
    p = proxies.get(parts.scheme) or proxies.get("all")
    if not p:
        return None
    try:
        if urllib.request.proxy_bypass(parts.hostname or ""):
            return None
    except Exception:  # pragma: no cover  个别平台读系统设置会出错
        pass
    return p


def _mask_proxy(p: Optional[str]) -> Optional[str]:
    """代理地址里可能带账号密码，记录时去掉。"""
    if not p:
        return p
    parts = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, IndexError):
        return None


def explain(kind: str, diag: Dict[str, Any]) -> str:
    """根据诊断信息猜原因（给人看的一句话）。"""
    atts = diag.get("attempts") or [{}]
    last = atts[-1]
    status = last.get("status")
    body = (last.get("body") or "").strip()
    hdr = {k.lower(): v for k, v in (last.get("headers") or {}).items()}
    proxy = diag.get("proxy")
    if proxy and status and not body:
        return (f"请求经过了代理 {proxy}，返回 {status} 且没有内容，多半是代理连不上模型服务。"
                "模型服务在本机或内网时，把 proxy 设成 auto（默认）或 none；在外网时检查代理能不能连到它。")
    if status and (hdr.get("via") or "proxy" in hdr.get("server", "").lower()):
        return f"返回 {status}，响应头显示中间有代理或网关（{hdr.get('via') or hdr.get('server')}），先确认代理/网关后面的模型服务是否正常。"
    if status == 503 and not body:
        return "返回 503 且没有内容，不像模型服务自己的报错（vLLM/SGLang 的报错带 JSON），多半是中间的代理或网关；可以用 --check 看看请求是否走了代理。"
    if status in (502, 504):
        return "网关报错：网关后面的模型服务没响应或超时。看看模型服务是否在运行、是否过载。"
    if status in (429, 503):
        return "服务端说忙或限流。降低 match.concurrency，或调大 http_retries / backoff。"
    if status == 401 or status == 403:
        return "鉴权失败：检查 api_key / api_key_env。"
    if status == 404:
        return "地址不对：检查 base_url（一般以 /v1 结尾）和模型名。"
    if kind == "timeout":
        return "请求超时：模型生成太慢或服务端排队，调大 timeout 或降低并发。"
    if kind == "connect":
        return "连不上：检查 base_url 的地址和端口，模型服务是否已经启动。"
    return ""


class OpenAIChatClient(ChatClient):
    """POST {base_url}/chat/completions。

    api_key 取值顺序：api_key 参数 → api_key_env 指定的环境变量（默认 OPENAI_API_KEY）→ 不带鉴权头。
    遇到 408/409/425/429/5xx 和网络错误按指数退避重试 http_retries 次，服务端给了 Retry-After 就按它等
    （最多 max_retry_after 秒）；4xx 其它错误直接失败。
    proxy：auto（默认：本机和内网地址不走代理，其它按系统代理设置）、system（完全按系统设置）、
           none（不走代理）、或者一个代理地址 http://host:port。
    extra_body 原样合并进请求体，用来传服务端特有参数（例如 vLLM 的 guided_json、top_k）。
    """

    default_url = DEFAULT_URLS["openai"]

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 api_key_env: str = "OPENAI_API_KEY", timeout: float = 60.0, http_retries: int = 3,
                 backoff: float = 1.0, max_retry_after: float = 60.0, proxy: Optional[str] = "auto",
                 headers: Optional[Dict[str, str]] = None,
                 extra_body: Optional[Dict[str, Any]] = None, path: str = "/chat/completions"):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env or "", "") or None
        self.timeout = timeout
        self.http_retries = int(http_retries)
        self.backoff = float(backoff)
        self.max_retry_after = float(max_retry_after)
        self.headers = dict(headers or {})
        self.extra_body = dict(extra_body or {})
        self.path = path
        self.proxy_mode = "auto" if proxy in (None, "", "auto") else str(proxy)
        self.proxy = self._resolve_proxy(self.base_url + self.path)
        handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy} if self.proxy else {})
        self._opener = urllib.request.build_opener(handler)

    def _resolve_proxy(self, url: str) -> Optional[str]:
        m = self.proxy_mode
        if m in ("none", "off", "false", "direct"):
            return None
        if m == "system":
            return system_proxy_for(url)
        if m == "auto":
            host = urllib.parse.urlsplit(url).hostname or ""
            return None if is_local_host(host) else system_proxy_for(url)
        return m if "://" in m else "http://" + m

    def describe(self) -> Dict[str, Any]:
        """这个客户端实际会怎么发请求（给诊断看）。"""
        url = self.base_url + self.path
        return {"url": url, "proxy_mode": self.proxy_mode, "proxy": _mask_proxy(self.proxy),
                "system_proxy": _mask_proxy(system_proxy_for(url)), "timeout": self.timeout,
                "http_retries": self.http_retries, "backoff": self.backoff}

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdr = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            hdr["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=hdr, method=method)
        return self._opener.open(req, timeout=self.timeout)

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._request("POST", self.path, body) as r:
            return json.loads(r.read().decode("utf-8"))

    def _notify(self, rec: Dict[str, Any], wait: Optional[float]) -> None:
        if self.observer is not None:
            try:
                self.observer(dict(rec, wait=wait, max=self.http_retries + 1, url=self.base_url + self.path,
                                   proxy=_mask_proxy(self.proxy)))
            except Exception:  # pragma: no cover  进度显示出错不影响请求
                pass

    @staticmethod
    def _headers(h) -> Dict[str, str]:
        if h is None:
            return {}
        return {k: v for k, v in h.items() if k.lower() in KEEP_HEADERS}

    def chat(self, messages, **params) -> ChatResult:
        body = {"messages": messages, **{k: v for k, v in params.items() if v is not None}, **self.extra_body}
        t0 = time.perf_counter()
        last: Any = None
        kind = "error"
        tries: List[Dict[str, Any]] = []
        diag: Dict[str, Any] = {"url": self.base_url + self.path, "proxy": _mask_proxy(self.proxy),
                                "proxy_mode": self.proxy_mode, "attempts": tries}
        for attempt in range(self.http_retries + 1):
            ta = time.perf_counter()
            rec: Dict[str, Any] = {"attempt": attempt + 1}
            retry_after = None
            try:
                resp = self._post(body)
                if tries:
                    rec.update(status=200, seconds=round(time.perf_counter() - ta, 3))
                    tries.append(rec)
                break
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:2000]
                except Exception:
                    detail = ""
                hdrs = self._headers(e.headers)
                last = f"HTTP {e.code}: {detail}"
                kind = f"HTTP {e.code}"
                retry_after = _retry_after(hdrs.get("Retry-After") or hdrs.get("retry-after"))
                rec.update(status=e.code, headers=hdrs, body=detail, seconds=round(time.perf_counter() - ta, 3))
                tries.append(rec)
                if e.code not in RETRY_STATUS and e.code < 500:
                    self._notify(rec, None)
                    diag["hint"] = explain(kind, diag)
                    raise ChatError(last, body, detail, kind, diag) from None
            except (socket.timeout, TimeoutError) as e:
                last, kind = f"超时（{self.timeout} 秒）：{e}", "timeout"
                rec.update(error=last, seconds=round(time.perf_counter() - ta, 3))
                tries.append(rec)
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                is_to = isinstance(reason, (socket.timeout, TimeoutError))
                last = f"{'超时' if is_to else '连接失败'}：{reason}"
                kind = "timeout" if is_to else "connect"
                rec.update(error=last, seconds=round(time.perf_counter() - ta, 3))
                tries.append(rec)
            except (ConnectionError, OSError) as e:
                last, kind = f"{type(e).__name__}: {e}", "connect"
                rec.update(error=last, seconds=round(time.perf_counter() - ta, 3))
                tries.append(rec)
            except ValueError as e:  # 返回的不是 JSON
                last, kind = f"返回无法解析：{e}", "bad_response"
                rec.update(error=last, seconds=round(time.perf_counter() - ta, 3))
                tries.append(rec)
            wait = None
            if attempt < self.http_retries:
                wait = self.backoff * (2 ** attempt) * (0.5 + random.random())
                if retry_after is not None:
                    wait = max(wait, min(retry_after, self.max_retry_after))
                rec["wait"] = round(wait, 3)
            self._notify(rec, wait)
            if wait is not None:
                time.sleep(wait)
        else:
            diag["hint"] = explain(kind, diag)
            raise ChatError(f"请求失败（{self.http_retries + 1} 次）：{last}", body, last, kind, diag)
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatError(f"返回里没有 choices[0].message：{str(resp)[:500]}", body, resp, "bad_response",
                            diag) from None
        return ChatResult(text=msg.get("content") or "", request=body, response=resp,
                          latency=time.perf_counter() - t0, usage=resp.get("usage"),
                          http_attempts=attempt + 1,
                          reasoning=msg.get("reasoning_content") or msg.get("reasoning"),
                          extra={"http": diag} if tries else {})

    def list_models(self) -> List[str]:
        """GET {base_url}/models；失败抛异常。"""
        with self._request("GET", "/models") as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]


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
