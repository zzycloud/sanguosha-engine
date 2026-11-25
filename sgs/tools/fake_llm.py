"""本地假模型服务：OpenAI 兼容的 /v1/chat/completions，不需要 GPU 也不花钱，用来测试接入流程。

    python -m sgs.tools.fake_llm --port 8000
    python -m sgs.arena --p0 "llm?model=fake&base_url=http://127.0.0.1:8000/v1" --p1 rule --games 4

它从提示词里读出“合法动作（共 N 个）”，按请求里的 model 名决定怎么回答：
  fake / fake-random  随机挑一个合法编号，回 {"reason": ..., "action": k}（同样的提示词结果相同）
  fake-think          先输出一段 <think>…</think> 再给 JSON
  fake-flaky          第一次回乱码，被要求重答后再给合法编号（测重试）
  fake-bad            永远回乱码（测兜底）
  fake-range          永远回超出范围的编号（测兜底）
  fake-500            永远返回 HTTP 500（测网络重试和兜底）
  fake-503            永远返回空内容的 HTTP 503，带 Retry-After: 1（模拟代理或网关挡住了请求，看诊断信息长什么样）
  fake-slow           先等 --slow 秒再回答（测超时）

--delay N 让所有模型每次都先等 N 秒左右，模拟真模型的速度，用来看终端进度和心跳。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

N_RE = re.compile(r"合法动作（共 (\d+) 个）")


def _answer(model: str, messages: List[Dict[str, str]]) -> str:
    first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
    m = N_RE.search(first_user)
    n = int(m.group(1)) if m else 1
    k = int(hashlib.sha256(first_user.encode("utf-8")).hexdigest(), 16) % n
    retried = sum(1 for x in messages if x.get("role") == "assistant") > 0
    ok = json.dumps({"reason": "（假模型）随便选的", "action": k}, ensure_ascii=False)
    if model == "fake-bad":
        return "我觉得应该先观察一下局势。"
    if model == "fake-range":
        return json.dumps({"action": n + 3})
    if model == "fake-flaky" and not retried:
        return "嗯……这个问题很复杂"
    if model == "fake-think":
        return f"<think>一共 {n} 个选项，我选 {k}。</think>\n{ok}"
    return ok


class FakeLLMServer:
    """进程内启动：with FakeLLMServer() as srv: srv.url -> http://127.0.0.1:端口/v1"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, slow: float = 2.0, api_key: Optional[str] = None,
                 delay: float = 0.0, jitter: float = 0.5):
        self.slow = slow
        self.delay = delay          # 所有对话请求都先等 delay 秒（±jitter 比例），模拟真模型的生成时间
        self.jitter = jitter
        self.api_key = api_key
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj: Any) -> None:
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # 客户端已经超时断开
                    pass

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    names = ["fake", "fake-think", "fake-flaky", "fake-bad", "fake-range", "fake-500", "fake-503", "fake-slow"]
                    return self._send(200, {"object": "list", "data": [{"id": x, "object": "model"} for x in names]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": {"message": "not found"}})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with srv.lock:
                    srv.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                if srv.api_key and self.headers.get("Authorization") != f"Bearer {srv.api_key}":
                    return self._send(401, {"error": {"message": "bad api key"}})
                if srv.delay > 0:
                    time.sleep(max(0.0, srv.delay * (1 + srv.jitter * (2 * random.random() - 1))))
                model = body.get("model", "fake")
                if model == "fake-503":
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    return
                if model == "fake-500":
                    return self._send(500, {"error": {"message": "fake internal error"}})
                if model == "fake-slow":
                    time.sleep(srv.slow)
                text = _answer(model, body.get("messages", []))
                prompt_tokens = sum(len(m.get("content", "")) for m in body.get("messages", []))
                self._send(200, {
                    "id": f"fake-{len(srv.requests)}", "object": "chat.completion", "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(text),
                              "total_tokens": prompt_tokens + len(text)},
                })

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        h, p = self.httpd.server_address[:2]
        return f"http://{h}:{p}/v1"

    def start(self) -> "FakeLLMServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地假模型服务（OpenAI 兼容）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--slow", type=float, default=2.0, help="fake-slow 模型等待的秒数")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="所有请求都先等这么多秒再回答，模拟真模型的速度（看终端进度、心跳用），例如 --delay 2")
    ap.add_argument("--jitter", type=float, default=0.5, help="--delay 的随机浮动比例，0.5 表示 ±50%%")
    ap.add_argument("--api-key", default=None, help="设置后要求请求带 Authorization: Bearer <key>")
    a = ap.parse_args(argv)
    srv = FakeLLMServer(a.host, a.port, a.slow, a.api_key, delay=a.delay, jitter=a.jitter)
    print(f"假模型服务：{srv.url}（Ctrl+C 退出）")
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
