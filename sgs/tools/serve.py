"""本地网页对战/观战：python -m sgs.tools.serve [--port 8000] [--human 0] [--generals 关羽 吕布]

浏览器打开 http://127.0.0.1:8000/?live=1&human=0 ，点击选项即可以 0 号位和规则 AI 对战；
human 设为 -1 则两边都是 AI，打开页面后可以逐步回看整局。只监听本机地址。
"""
import argparse
import json
import os
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..agents import RuleAgent
from ..core.game import Game
from ..replay import Recorder

WEB = os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html")


class Session:
    def __init__(self, human, generals=None, seed=None):
        self.human = human
        seed = random.randrange(10 ** 6) if seed is None else seed
        kw = {"generals": generals} if generals else {"pick": "random"}
        self.g = Game("duel", seed=seed, auto_single=False, **kw)
        self.agents = [RuleAgent(seed), RuleAgent(seed + 1)]
        if human is not None:
            self.agents[human] = None
        self.rec = Recorder(self.g)
        self.rec.run(self.agents)

    def act(self, i):
        g = self.g
        if not g.over and g.pending.player == self.human and 0 <= i < len(g.pending.options):
            self.rec.act(i)
            self.rec.run(self.agents)

    def replay(self):
        g = self.g
        rep = self.rec.export()
        viewer = self.human
        if viewer is not None and not g.over:  # 对战进行中：不把 AI 的手牌和私密日志发给浏览器
            steps = []
            for s in rep["steps"]:
                s = dict(s, state=_hide(s["state"], viewer), logs=[_hide_log(e, viewer) for e in s["logs"]])
                if s["decision"] and s["decision"]["player"] != viewer:
                    s["decision"] = dict(s["decision"], options=["（对手的选项不可见）"])
                    s["chosen"] = None
                steps.append(s)
            rep["steps"] = steps
            rep["hidden_for"] = viewer
        return rep


def _hide(state, viewer):
    st = dict(state)
    st["players"] = [dict(p) for p in state["players"]]
    for p in st["players"]:
        if p["seat"] != viewer:
            p.pop("hand", None)
    st.pop("deck_top", None)
    st.pop("decision", None)
    return st


def _hide_log(e, viewer):
    if "private" not in e:
        return e
    e = dict(e)
    priv = {k: v for k, v in e["private"].items() if int(k) == viewer}
    if priv:
        e["private"] = priv
    else:
        e.pop("private")
    return e


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/replay":
                return self._json(state["s"].replay())
            if u.path == "/api/new":
                q = parse_qs(u.query)
                gens = q.get("generals", [None])[0]
                state["s"] = Session(state["human"], gens.split(",") if gens else state["generals"])
                return self._json({"ok": True})
            if u.path in ("/", "/index.html"):
                with open(WEB, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/act":
                state["s"].act(int(data["index"]))
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--human", type=int, default=0, help="人类控制的座位，-1 表示 AI 对 AI")
    ap.add_argument("--generals", nargs=2, default=None)
    a = ap.parse_args()
    human = None if a.human < 0 else a.human
    state = {"human": human, "generals": a.generals, "s": Session(human, a.generals)}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state))
    q = f"?live=1&human={human}" if human is not None else "?live=1"
    print(f"打开 http://127.0.0.1:{a.port}/{q}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
