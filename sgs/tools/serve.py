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

from ..agents import make_agent
from ..core.game import Game
from ..replay import Recorder

WEB = os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html")


class Session:
    def __init__(self, human, generals=None, seed=None, ai=("rule", "rule")):
        self.human = human
        seed = random.randrange(10 ** 6) if seed is None else seed
        kw = {"generals": generals} if generals else {"pick": "random"}
        self.rec = Recorder.new_game("duel", seed=seed, auto_single=False, **kw)
        self.g = self.rec.g
        self.agents = [make_agent(ai[0], seed=seed), make_agent(ai[1], seed=seed + 1)]
        for s, ag in enumerate(self.agents):
            ag.on_game_start({"game_id": f"live-{seed}", "seat": s, "seed": seed, "mode": "duel"})
        if human is not None:
            self.agents[human] = None
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
            last = len(rep["steps"]) - 1
            for k, s in enumerate(rep["steps"]):
                logs = [e for e in (_hide_log(e, viewer) for e in s["logs"]) if e.get("text") or e.get("private")]
                if s["type"] == "event" and not logs:
                    continue  # 只对 AI 可见的事件（例如 AI 没有使用无懈可击）整步不发
                if s["type"] == "decision" and s["decision"]["player"] != viewer and k != last:
                    continue  # AI 被问了什么也不发，免得从“被询问”推断出它有某张牌
                s = dict(s, state=_hide(s["state"], viewer), logs=logs)
                if s["decision"] and s["decision"]["player"] != viewer:
                    s["decision"] = dict(s["decision"], prompt="", options=["（对手的选项不可见）"])
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
                state["s"] = Session(state["human"], gens.split(",") if gens else state["generals"], ai=state["ai"])
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


def main(argv=None):
    from .. import config as C
    ap = argparse.ArgumentParser(description="本地网页对战 / 观战。对手可以是任意智能体，写在配置文件的 players 里")
    ap.add_argument("config", nargs="?", default=None,
                    help="配置文件（同 sgs.arena）：players.A 坐 0 号位，players.B 坐 1 号位；match.generals 指定武将")
    ap.add_argument("--set", "-s", action="append", default=[], metavar="路径=值", help="覆盖配置")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--human", type=int, default=0, help="人类控制的座位（0 或 1），-1 表示看两个 AI 对打")
    ap.add_argument("--generals", nargs=2, default=None, help="等于 --set match.generals=[A,B]")
    a = ap.parse_args(argv)
    sets = list(a.set) + ([f"match.generals=[{a.generals[0]}, {a.generals[1]}]"] if a.generals else [])
    cfg = C.load(a.config, sets)
    human = None if a.human < 0 else a.human
    ai = (C.agent_spec(cfg["players"]["A"]), C.agent_spec(cfg["players"]["B"]))
    gens = C.generals_list(cfg)
    state = {"human": human, "generals": gens, "ai": ai, "s": Session(human, gens, ai=ai)}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state))
    q = f"?live=1&human={human}" if human is not None else "?live=1"
    print(f"打开 http://127.0.0.1:{a.port}/{q}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
