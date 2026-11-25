"""智能体框架：注册表、提示词、解析、LLMAgent 的重试/兜底/记录、对战命令行（用本地假模型，不联网）。"""
import json
import os

import pytest

from sgs.agents import Agent, RuleAgent, decide, make_agent, parse_spec, register_agent
from sgs.agents.llm import LLMAgent, PromptBuilder, parse_action
from sgs.arena import main as arena_main, run_arena
from sgs.core.game import Game
from sgs.core.registry import load_all
from sgs.tools.fake_llm import FakeLLMServer

load_all()


@pytest.fixture(scope="module")
def srv():
    with FakeLLMServer(slow=0.5) as s:
        yield s


def llm(srv, model="fake", **kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    return f"llm?model={model}&base_url={srv.url}" + (f"&{q}" if q else "")


# ------------------------------------------------------------------ 注册表
def test_parse_spec_and_config_file(tmp_path):
    kind, cfg = parse_spec("llm?model=q&base_url=http://h:8000/v1&temperature=0.2&max_retries=3&ask_reason=true")
    assert kind == "llm" and cfg == {"model": "q", "base_url": "http://h:8000/v1", "temperature": 0.2,
                                     "max_retries": 3, "ask_reason": True}
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"model": "a", "temperature": 0.9}), encoding="utf-8")
    kind, cfg = parse_spec(f"llm@{p}?temperature=0")
    assert kind == "llm" and cfg == {"model": "a", "temperature": 0}


def test_registry_builtin_custom_and_module_path():
    assert isinstance(make_agent("rule", seed=1), RuleAgent)

    @register_agent("always-last")
    class Last(Agent):
        def __init__(self, seed=None):
            pass

        def choose(self, view, d):
            return len(d.options) - 1
    assert isinstance(make_agent("always-last"), Last)
    assert type(make_agent("sgs.agents.rule:RuleAgent", seed=3)).__name__ == "RuleAgent"
    with pytest.raises(KeyError):
        make_agent("no-such-agent")
    with pytest.raises(TypeError):
        make_agent("llm?model=x&no_such_option=1")


def test_decide_rejects_illegal_index():
    class Bad(Agent):
        def choose(self, view, d):
            return 99
    g = Game("duel", seed=1, generals=["关羽", "貂蝉"])
    with pytest.raises(ValueError):
        decide(Bad(), g)


# ------------------------------------------------------------------ 提示词 / 解析
def test_prompt_shows_own_hand_not_opponents():
    g = Game("duel", seed=3, generals=["关羽", "貂蝉"])
    d = g.pending
    msgs = PromptBuilder().messages(g.view(d.player, with_logs=True, derived=True), d)
    text = msgs[1]["content"]
    assert msgs[0]["role"] == "system" and '"action"' in msgs[0]["content"]
    me, opp = g.players[d.player], g.players[1 - d.player]
    for c in me.hand:
        assert g.cards[c].name in text
    assert f"手牌数：{len(opp.hand)}" in text
    assert f"合法动作（共 {len(d.options)} 个）" in text
    for i, o in enumerate(d.options):
        assert f"{i}. {o.label}" in text
    assert "闭月：回合结束阶段，你可以摸一张牌。" in text
    # 对手手牌不在提示词里（只看对手私有、我方不可能看到的牌）
    mine = {g.cards[c].id for c in me.hand}
    hidden = [g.cards[c] for c in opp.hand if c not in mine]
    assert all(f"{c.suit}{c.rank if c.rank not in (1, 11, 12, 13) else 'AJQK'[[1, 11, 12, 13].index(c.rank)]}{c.name}"
               not in text for c in hidden)


@pytest.mark.parametrize("text,n,want", [
    ('{"action": 2}', 5, 2),
    ('<think>想想……选 4？不，选 1</think>{"reason": "打杀", "action": 1}', 5, 1),
    ('```json\n{"action": "3"}\n```', 5, 3),
    ("选择：3", 5, 3),
    ("2", 5, 2),
    ("结束出牌阶段", 3, 2),
    ('{"action": 7}', 5, None),
    ("我想想", 5, None),
    ("<think>没想完", 5, None),
])
def test_parse_action(text, n, want):
    i, err, _ = parse_action(text, n, ["a", "b", "结束出牌阶段"][:n] if n == 3 else [])
    assert i == want and (err is None) == (want is not None)


# ------------------------------------------------------------------ LLMAgent
def _agent_decision(srv, model, **kw):
    ag = make_agent(llm(srv, model, **kw), seed=1)
    calls = []
    ag.sink = calls.append
    g = Game("duel", seed=3, generals=["关羽", "貂蝉"])
    i = decide(ag, g)
    return ag, calls, i, g


def test_llm_agent_valid_and_transcript(srv):
    ag, calls, i, g = _agent_decision(srv, "fake-think", api_key="sk-local")
    assert ag.last_info["source"] == "llm" and ag.last_info["attempts"] == 1
    rec = calls[0]
    assert rec["format"] == "sgs-llm-call/1" and rec["action"] == i
    assert rec["attempts"][0]["messages"][0]["role"] == "system"
    assert "<think>" in rec["attempts"][0]["text"]
    assert "sk-local" not in json.dumps(rec, ensure_ascii=False)       # 密钥不进记录
    assert srv.requests[-1]["auth"] == "Bearer sk-local"


def test_llm_agent_retries_then_succeeds(srv):
    ag, calls, i, g = _agent_decision(srv, "fake-flaky")
    att = calls[0]["attempts"]
    assert ag.last_info["source"] == "llm" and len(att) == 2
    assert att[0]["error"] and att[1]["error"] is None
    assert att[1]["messages"][-1]["role"] == "user" and "不合法" in att[1]["messages"][-1]["content"]


@pytest.mark.parametrize("model,extra", [("fake-bad", {}), ("fake-range", {}),
                                         ("fake-500", {"http_retries": 1, "backoff": 0}),
                                         ("fake-slow", {"timeout": 0.1, "http_retries": 0})])
def test_llm_agent_falls_back_to_rule(srv, model, extra):
    ag, calls, i, g = _agent_decision(srv, model, **extra)
    assert ag.last_info["source"] == "fallback" and ag.last_info["errors"]
    ref = RuleAgent(1).choose(g.view(g.pending.player), g.pending)
    assert i == ref and ag.stats["fallbacks"] == 1


def test_llm_agent_without_fallback_raises(srv):
    with pytest.raises(RuntimeError):
        _agent_decision(srv, "fake-bad", fallback="none", max_retries=0)


def test_llm_agent_skips_single_option(srv):
    from sgs.core.base import Decision, Option
    n0 = len(srv.requests)
    ag = make_agent(llm(srv), seed=0)
    g = Game("duel", seed=3, generals=["关羽", "貂蝉"])
    d = Decision(0, "choose", "只有一个选项", [Option(("ok",), "好")])
    assert ag.choose(g.view(0), d) == 0 and ag.last_info == {"source": "single", "auto": "唯一选项"} and len(srv.requests) == n0


# ------------------------------------------------------------------ 对战
def test_arena_concurrent_with_fake_model(srv, tmp_path):
    out = tmp_path / "run"
    s = run_arena(llm(srv, ask_reason="true"), "rule", games=4, concurrency=4, swap=True, out=str(out),
                  replays="2", progress=False, write_decisions=True, write_llm_calls=True)
    assert s["games"] == 4 and s["errors"] == 0
    assert s["sides"]["A"]["games_first"] == 2 and s["sides"]["A"]["llm"]["fallbacks"] == 0
    assert sum(v["wins"] for v in s["sides"].values()) + s["draws_or_aborted"] == 4
    files = set(os.listdir(out))
    assert {"index.html", "summary.json", "games.json", "decisions.jsonl", "llm_calls.jsonl", "cards.json", "games"} <= files
    assert sorted(os.listdir(out / "games")) == ["0001", "0002", "0003", "0004"]
    assert [os.path.exists(out / "games" / d / "replay.html") for d in ("0001", "0002", "0003")] == [True, True, False]
    rows = [json.loads(x) for x in open(out / "decisions.jsonl", encoding="utf-8")]
    llm_rows = [r for r in rows if r["agent"].startswith("llm")]
    assert llm_rows and all(r["agent_info"]["source"] == "llm" for r in llm_rows)
    assert all(r["agent_info"]["reason"] for r in llm_rows)
    calls = [json.loads(x) for x in open(out / "llm_calls.jsonl", encoding="utf-8")]
    assert len(calls) == len(llm_rows)
    # 合并的 JSONL 里一局的行是连在一起的，不和别的局交错
    ids = [c["game_id"] for c in calls]
    assert all(ids.index(x) + ids.count(x) - 1 == len(ids) - 1 - ids[::-1].index(x) for x in set(ids))
    games = json.load(open(out / "games.json", encoding="utf-8"))
    # 交换先后手：每两局同一种子同一对武将
    by = sorted(games, key=lambda r: r["game"])
    assert [r["game"] for r in by] == [1, 2, 3, 4] and by[0]["dir"] == "games/0001"
    assert by[0]["seed"] == by[1]["seed"] and by[0]["generals"] == by[1]["generals"]
    assert by[0]["seats"] == [0, 1] and by[1]["seats"] == [1, 0]


def _embedded_replay(path):
    import re
    html = open(path, encoding="utf-8").read()
    m = re.search(r'id="embedded-replay" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_arena_per_game_dir_is_aligned(srv, tmp_path):
    """每局目录里的录像、轨迹、模型调用用 replay_step / step / call 对齐。"""
    out = tmp_path / "run"
    s = run_arena(llm(srv, "fake-flaky"), "rule", games=1, out=str(out), progress=False)
    gd = out / "games" / "0001"
    assert set(os.listdir(gd)) == {"replay.html", "game.md", "trajectory.json", "llm_calls.json", "result.json"}
    traj = json.load(open(gd / "trajectory.json", encoding="utf-8"))
    calls = json.load(open(gd / "llm_calls.json", encoding="utf-8"))
    rep = _embedded_replay(gd / "replay.html")
    assert rep["llm_calls"] == calls and rep["agents"] == traj["agents"]
    assert [c["call"] for c in calls] == list(range(len(calls)))
    seen = []
    for row in traj["decisions"]:
        st = rep["steps"][row["replay_step"]]
        assert st["type"] == "decision" and st["chosen"] == row["action"]
        assert st["decision"]["options"] == [a["label"] for a in row["legal_actions"]]
        assert st.get("calls", []) == row["llm_calls"] and st["agent"] == row["agent"]
        for ci in row["llm_calls"]:
            c = calls[ci]
            assert c["replay_step"] == row["replay_step"] and c["decision_step"] == row["step"]
            assert c["action"] == row["action"] and c["legal_actions"] == st["decision"]["options"]
            seen.append(ci)
    assert sorted(seen) == list(range(len(calls))) and calls
    # fake-flaky 每次先答错一次：轨迹、汇总、索引页里都能看到
    r = s["results"][0]
    assert r["illegal"] == len(calls) and r["llm_requests"] == 2 * len(calls) and r["fallbacks"] == 0
    assert all(c["attempts"][0]["error_kind"] == "illegal" for c in calls)
    md = (gd / "game.md").read_text(encoding="utf-8")
    assert f"录像第 {calls[0]['replay_step']} 步" in md and "模型的原始回复" in md and "答错或失败：" in md
    assert "← **选择**" in md
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "games/0001/replay.html" in idx and "games/0001/game.md" in idx
    res = json.load(open(gd / "result.json", encoding="utf-8"))
    assert res["llm_calls"] == len(calls) and res["dir"] == "games/0001"


def test_arena_can_skip_per_game_files(tmp_path):
    out = tmp_path / "run"
    run_arena("rule", "random", games=2, out=str(out), progress=False, per_game=False, write_decisions=True)
    files = set(os.listdir(out))
    assert "games" not in files and {"decisions.jsonl", "index.html", "summary.json"} <= files


def test_replay_viewer_has_llm_panel():
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    for name in ("function agentHTML", "function attemptHTML", "模型的原始回复", "下个出问题的", "#step="):
        assert name in html


def test_arena_game_timeout_and_cli(srv, tmp_path, capsys):
    s = run_arena(llm(srv, "fake-slow", timeout=5, decision_timeout=1), "rule", games=1, game_timeout=0.2,
                  out=None, progress=False)
    assert s["results"][0]["result"].startswith("超过") and s["draws_or_aborted"] == 1
    arena_main(["--set", "players.B.agent=random", "--set", "match.games=2", "--set", "match.concurrency=2",
                "--set", "output.replays=none", "-o", str(tmp_path / "cli")])
    assert "A rule" in capsys.readouterr().out


# ------------------------------------------------------------------ 配置文件
from sgs import config as C  # noqa: E402
from sgs.arena import run_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("name", sorted(os.listdir(os.path.join(ROOT, "configs"))))
def test_example_configs_load_and_build_agents(name):
    path = os.path.join(ROOT, "configs", name)
    if os.path.isdir(path):
        return
    if "variants" in C.read_file(path):          # 对比实验的配置
        from sgs import experiment as E
        cfgs = list(E.load(path)["_configs"].values())
    else:
        cfgs = [C.load(path)]
    for cfg in cfgs:
        for side in "AB":
            make_agent(C.agent_spec(cfg["players"][side]), seed=0).close()


def test_config_defaults_overrides_and_errors(tmp_path):
    cfg = C.load(None, ["match.games=3", "players.A.agent=random", "match.generals=[关羽, 吕布]"])
    assert cfg["match"]["games"] == 3 and cfg["match"]["swap"] is True
    assert cfg["players"]["A"] == {"agent": "random"} and C.generals_list(cfg) == ["关羽", "吕布"]
    for bad in (["match.gmaes=3"], ["players.C.agent=rule"], ["match.generals=[关羽, 不存在]"], ["oops"]):
        with pytest.raises(C.ConfigError):
            C.load(None, bad)
    (tmp_path / "m.yaml").write_text("agent: llm\nmodel: base\ntemperature: 0.7\n", encoding="utf-8")
    (tmp_path / "x.yaml").write_text(
        "players:\n  A:\n    include: m.yaml\n    temperature: 0\n    api_key: ${SGS_TEST_KEY}\n  B: rule\n",
        encoding="utf-8")
    os.environ["SGS_TEST_KEY"] = "sk-secret"
    cfg = C.load(str(tmp_path / "x.yaml"))
    spec = C.agent_spec(cfg["players"]["A"])
    assert spec == {"kind": "llm", "model": "base", "temperature": 0, "api_key": "sk-secret"}
    assert C.agent_spec(cfg["players"]["B"]) == "rule"
    assert "sk-secret" not in C.dump_yaml(C.redacted(cfg))


def test_run_config_with_fake_model(srv, tmp_path):
    cfg = C.load(os.path.join(ROOT, "configs", "fake-model.yaml"),
                 [f"players.A.base_url={srv.url}", f"output.dir={tmp_path}/{{name}}", "output.progress=false",
                  "output.llm_calls=true"])
    s = run_config(cfg)
    out = tmp_path / "fake-model"
    assert s["errors"] == 0 and s["sides"]["A"]["llm"]["fallbacks"] == 0
    files = set(os.listdir(out))
    assert "config.yaml" in files and "llm_calls.jsonl" in files and "decisions.jsonl" not in files
    assert "index.html" in files and len(os.listdir(out / "games")) == cfg["match"]["games"]
    assert "agent: llm" in (out / "config.yaml").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 请求失败：代理、重试、诊断
import http.server  # noqa: E402
import threading  # noqa: E402

from sgs.agents.llm.client import ChatError, OpenAIChatClient, is_local_host  # noqa: E402


@pytest.fixture
def bad_proxy():
    """一个只会回空内容 503 的“代理”，模拟系统代理连不上模型服务。"""
    class P(http.server.BaseHTTPRequestHandler):
        hits = 0

        def log_message(self, *a):
            pass

        def _r(self):
            P.hits += 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.send_header("Server", "mihomo")
            self.end_headers()
        do_POST = do_GET = _r
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s, P
    s.shutdown()


@pytest.fixture
def env_proxy(monkeypatch, bad_proxy):
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("http_proxy", f"http://user:secret@127.0.0.1:{bad_proxy[0].server_address[1]}")
    return bad_proxy


def test_is_local_host():
    for h in ("localhost", "127.0.0.1", "::1", "10.1.2.3", "192.168.0.8", "172.20.0.1", "gpu.local", "0.0.0.0"):
        assert is_local_host(h), h
    for h in ("api.openai.com", "8.8.8.8", "172.32.0.1", ""):
        assert not is_local_host(h), h


def test_local_server_bypasses_system_proxy_by_default(srv, env_proxy):
    _, P = env_proxy
    c = OpenAIChatClient(base_url=srv.url, backoff=0.01)
    assert c.proxy is None and c.chat([{"role": "user", "content": "x"}], model="fake").text
    assert P.hits == 0


def test_proxy_503_is_diagnosed(srv, env_proxy):
    """复现用户看到的“请求失败：HTTP 503: ”：本地请求被系统代理转走，代理回空内容 503。"""
    _, P = env_proxy
    c = OpenAIChatClient(base_url=srv.url, backoff=0.001, proxy="system", http_retries=2)
    with pytest.raises(ChatError) as ei:
        c.chat([{"role": "user", "content": "x"}], model="fake")
    e = ei.value
    assert str(e).startswith("请求失败（3 次）：HTTP 503") and e.kind == "HTTP 503" and P.hits == 3
    d = e.diagnostics
    assert d["proxy"].startswith("http://127.0.0.1:") and "secret" not in json.dumps(d)
    assert [a["status"] for a in d["attempts"]] == [503, 503, 503]
    assert d["attempts"][0]["headers"]["Server"] == "mihomo" and d["attempts"][0]["body"] == ""
    assert "代理" in d["hint"]
    # 显式写 none 也不走代理
    assert OpenAIChatClient(base_url=srv.url, proxy="none").chat([{"role": "user", "content": "x"}], model="fake").text


def test_retry_after_is_respected(srv, monkeypatch):
    waits = []
    monkeypatch.setattr("sgs.agents.llm.client.time.sleep", lambda t: waits.append(t))
    c = OpenAIChatClient(base_url=srv.url, backoff=0.001, http_retries=2, max_retry_after=0.5)
    with pytest.raises(ChatError) as ei:
        c.chat([{"role": "user", "content": "x"}], model="fake-503")     # Retry-After: 1，封顶 0.5
    assert waits and all(abs(w - 0.5) < 1e-9 for w in waits)
    assert "没有内容" in ei.value.diagnostics["hint"] or "代理" in ei.value.diagnostics["hint"]


def test_request_errors_are_counted_and_recorded(srv, tmp_path):
    out = tmp_path / "run"
    s = run_arena(llm(srv, "fake-503", backoff=0.001, max_retry_after=0, http_retries=1), "rule", games=1,
                  out=str(out), progress=False)
    L = s["sides"]["A"]["llm"]
    assert L["error_kinds"] == {"HTTP 503": L["errors"]} and L["fallbacks"] == L["decisions"] > 0
    calls = json.load(open(out / "games" / "0001" / "llm_calls.json", encoding="utf-8"))
    a = calls[0]["attempts"][0]
    assert a["error_kind"] == "HTTP 503" and a["http"]["attempts"][0]["headers"]["Retry-After"] == "1"
    assert calls[0]["source"] == "fallback" and calls[0]["concurrency"] == 1
    assert "HTTP 503" in (out / "index.html").read_text(encoding="utf-8")


def test_check_connection(srv, env_proxy, capsys):
    from sgs.agents.llm import check_connection, format_check
    ok = check_connection(make_agent(llm(srv), seed=0))
    assert ok["ok"] and ok["proxy"] is None and "fake" in ok["models"]
    assert "不走代理" in format_check(ok)
    bad = check_connection(make_agent(llm(srv, proxy="system", backoff=0.001, http_retries=0), seed=0))
    assert not bad["ok"] and bad["error_kind"] == "HTTP 503" and "代理" in bad["hint"]
    txt = format_check(bad)
    assert "不通" in txt and "mihomo" in txt and "可能的原因" in txt
    warn = check_connection(make_agent(llm(srv, "no-such-model"), seed=0))
    assert "no-such-model" in warn.get("warning", "")
    with pytest.raises(SystemExit) as ei:
        arena_main(["--check", "--set", "players.A.agent=llm", "--set", f"players.A.base_url={srv.url}",
                    "--set", "players.A.model=fake", "--set", "players.A.proxy=system",
                    "--set", "players.A.http_retries=0"])
    assert ei.value.code == 1 and "可能的原因" in capsys.readouterr().err


# ------------------------------------------------------------------ 终端进度、边打边写、中止
import io  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from sgs.arena import ArenaError, play_one  # noqa: E402
from sgs.progress import Progress, fmt_secs, level_of  # noqa: E402


def test_progress_levels_and_format():
    assert level_of(None) == 1 and level_of(True) == 1 and level_of(False) == 0 and level_of("Verbose") == 2
    with pytest.raises(ValueError):
        level_of("loud")
    assert fmt_secs(5) == "5 秒" and fmt_secs(125) == "2 分 05 秒" and fmt_secs(3700) == "1 小时 01 分"
    with pytest.raises(C.ConfigError):
        C.load(data={"output": {"progress": "loud"}})


def test_arena_prints_start_end_and_summary(srv, tmp_path):
    buf = io.StringIO()
    s = run_arena(llm(srv), "rule", games=2, concurrency=2, out=str(tmp_path / "r"), stream=buf,
                  heartbeat=0, connect_check=True)
    txt = buf.getvalue()
    assert "对战开始：2 局" in txt and "连通性检查 A：通" in txt
    assert txt.count("[开始]") == 2 and txt.count("[结束]") == 2 and "总比分 A" in txt
    assert s["games"] == 2
    quiet = io.StringIO()
    run_arena("rule", "random", games=2, out=None, stream=quiet, progress="quiet")
    q = quiet.getvalue()
    assert "对战开始" in q and "[开始]" not in q and "[结束]" not in q
    verbose = io.StringIO()
    run_arena(llm(srv), "rule", games=1, out=None, stream=verbose, progress="verbose", heartbeat=0)
    assert "（模型，" in verbose.getvalue()


def test_heartbeat_shows_running_games(tmp_path):
    with FakeLLMServer(delay=0.15, jitter=0) as slow:
        buf = io.StringIO()
        t0 = time.perf_counter()
        run_arena(llm(slow), "rule", games=1, out=None, stream=buf, heartbeat=0.3, game_timeout=1.5)
        assert time.perf_counter() - t0 >= 1.0
    beats = [l for l in buf.getvalue().splitlines() if l.startswith("[进度")]
    assert beats, buf.getvalue()
    body = buf.getvalue().split(beats[-1], 1)[1]
    assert "完成 0/1" in beats[-1] and "第 1 局" in body and "个决策" in body
    assert "[超时] 第 1 局" in buf.getvalue()


def test_request_failures_are_printed_immediately(srv):
    buf = io.StringIO()
    run_arena(llm(srv, "fake-503", backoff=0.001, max_retry_after=0, http_retries=1), "rule", games=1,
              out=None, stream=buf, heartbeat=0, game_timeout=5)
    txt = buf.getvalue()
    assert "[请求失败] 第 1 局" in txt and "HTTP 503，响应体为空" in txt and "秒后重试（第 2/2 次）" in txt
    assert "不再重试，交给兜底" in txt and "[兜底] 第 1 局" in txt


def test_connect_check_failure_stops_before_playing(srv, tmp_path):
    buf = io.StringIO()
    with pytest.raises(ArenaError):
        run_arena(llm(srv, "fake-503", backoff=0.001, max_retry_after=0, http_retries=0), "rule", games=1,
                  out=str(tmp_path / "r"), stream=buf, connect_check=True)
    assert "不通" in buf.getvalue() and not (tmp_path / "r" / "games").exists()


def test_files_are_written_while_playing_and_stop_keeps_them(tmp_path):
    d = tmp_path / "g"
    stop = threading.Event()
    box = {}
    with FakeLLMServer(delay=0.05, jitter=0) as slow:
        th = threading.Thread(target=lambda: box.update(info=play_one(
            1, 0, [llm(slow), "rule"], [0, 1], ["张飞", "黄盖"], game_dir=str(d), stop=stop)))
        th.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                md = (d / "game.md").read_text(encoding="utf-8") if (d / "game.md").exists() else ""
                if md.count("### 录像第") >= 3 and (d / "trajectory.json").exists():
                    break
                time.sleep(0.05)
            # 打到一半：result.json 是占位，game.md 已经有决策点，trajectory.json 是完整 JSON
            assert json.loads((d / "result.json").read_text(encoding="utf-8"))["result"] == "进行中…"
            assert md.count("### 录像第") >= 3
            mid = json.loads((d / "trajectory.json").read_text(encoding="utf-8"))
            assert mid["result"] == "进行中…" and mid["decisions"]
            assert not (d / "replay.html").exists()
        finally:
            stop.set()
            th.join(30)
    info = box["info"]
    assert info["result"] == "手动中止（Ctrl+C）" and info["winner_seat"] is None
    traj = json.loads((d / "trajectory.json").read_text(encoding="utf-8"))
    assert traj["result"] == "手动中止（Ctrl+C）" and len(traj["decisions"]) >= len(mid["decisions"])
    assert json.loads((d / "result.json").read_text(encoding="utf-8"))["result"] == "手动中止（Ctrl+C）"
    assert (d / "replay.html").exists() and "## 结果" in (d / "game.md").read_text(encoding="utf-8")


def test_auto_decisions_are_marked_in_trajectory(srv, tmp_path):
    out = tmp_path / "r"
    run_arena(llm(srv), "rule", games=1, out=str(out), progress="quiet")
    traj = json.load(open(out / "games" / "0001" / "trajectory.json", encoding="utf-8"))
    auto = traj["auto_decisions"]
    assert auto and all(a["auto"] in ("唯一选项", "剩下的牌都要选") for a in auto)
    steps = {r["step"] for r in traj["decisions"]}
    assert not steps & {a["step"] for a in auto}
    calls = json.load(open(out / "games" / "0001" / "llm_calls.json", encoding="utf-8"))
    assert all(len(c["legal_actions"]) > 1 for c in calls) and calls[0]["template"] == "default"


def test_experiment_compares_variants(srv, tmp_path):
    from sgs import experiment as E
    sets = ["base.match.games=2", f"base.players.A.base_url={srv.url}", "base.players.A.model=fake",
            f"dir={tmp_path / 'exp'}", "base.output.progress=quiet"]
    exp = E.load(os.path.join(ROOT, "configs", "compare-prompts.yaml"), sets)
    assert list(exp["_configs"]) == ["old", "new"]
    assert exp["_configs"]["new"]["players"]["A"]["prompt"] == "v2"
    res = E.run(exp, stream=io.StringIO())
    rows = json.load(open(tmp_path / "exp" / "comparison.json", encoding="utf-8"))["rows"]
    assert [r["variant"] for r in rows] == ["old", "new"] and all(r["games"] == 2 for r in rows)
    assert rows[0]["avg_prompt_tokens"] > rows[1]["avg_prompt_tokens"] > 0
    assert (tmp_path / "exp" / "new" / "index.html").exists()
    assert "胜率（95% 区间）" in (tmp_path / "exp" / "comparison.md").read_text(encoding="utf-8")
    assert res["rows"] == rows
    with pytest.raises(C.ConfigError):
        E.load(os.path.join(ROOT, "configs", "compare-prompts.yaml"), ["base.match.games=3"])
    assert E.wilson(50, 100)[0] < 0.5 < E.wilson(50, 100)[1]
