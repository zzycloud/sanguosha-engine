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
from ..replay import decision_dict

WEB = os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html")


class Session:
    def __init__(self, human, generals=None, seed=None):
        self.human = human
        seed = random.randrange(10 ** 6) if seed is None else seed
        kw = {"generals": generals} if generals else {}
        self.g = Game("duel", seed=seed, **kw)
        self.agents = [RuleAgent(seed), RuleAgent(seed + 1)]
        self.steps, self.seen = [], 0
        self.advance()

    def _record(self, i):
        g = self.g
        self.steps.append({"state": g.observe(None), "logs": g.logs[self.seen:],
                           "decision": decision_dict(g.pending), "chosen": i})
        self.seen = len(g.logs)
        g.act(i)

    def advance(self):
        g = self.g
        while not g.over and g.pending.player != self.human:
            self._record(self.agents[g.pending.player].choose(g, g.pending))

    def act(self, i):
        if not self.g.over and self.g.pending.player == self.human:
            self._record(i)
            self.advance()

    def replay(self):
        g = self.g
        viewer = None if self.human is None else self.human
        cur = {"state": g.observe(None), "logs": g.logs[self.seen:],
               "decision": decision_dict(g.pending) if g.pending else None, "chosen": None}
        steps = self.steps + [cur]
        if viewer is not None and not g.over:  # 对战时隐藏 AI 手牌
            steps = [dict(s, state=_hide(s["state"], viewer)) for s in steps]
            steps[-1]["logs"] = [_hide_log(e, viewer) for e in steps[-1]["logs"]]
            for s in steps[:-1]:
                s["logs"] = [_hide_log(e, viewer) for e in s["logs"]]
        return {"format": "sgs-replay/1", "seed": g.seed, "mode": "duel",
                "generals": [p.general for p in g.players], "winner": g.winner, "result": g.result, "steps": steps}


def _hide(state, viewer):
    st = dict(state)
    st["players"] = [dict(p) for p in state["players"]]
    for p in st["players"]:
        if p["seat"] != viewer:
            p.pop("hand", None)
    st.pop("deck_top", None)
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
