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
    assert ag.choose(g.view(0), d) == 0 and ag.last_info == {"source": "single"} and len(srv.requests) == n0


# ------------------------------------------------------------------ 对战
def test_arena_concurrent_with_fake_model(srv, tmp_path):
    out = tmp_path / "run"
    s = run_arena(llm(srv, ask_reason="true"), "rule", games=4, concurrency=4, swap=True, out=str(out),
                  replays="2", progress=False)
    assert s["games"] == 4 and s["errors"] == 0
    assert s["sides"]["A"]["games_first"] == 2 and s["sides"]["A"]["llm"]["fallbacks"] == 0
    assert sum(v["wins"] for v in s["sides"].values()) + s["draws_or_aborted"] == 4
    files = set(os.listdir(out))
    assert {"summary.json", "games.jsonl", "decisions.jsonl", "llm_calls.jsonl", "cards.json", "replays"} <= files
    assert len(os.listdir(out / "replays")) == 2
    rows = [json.loads(x) for x in open(out / "decisions.jsonl", encoding="utf-8")]
    llm_rows = [r for r in rows if r["agent"].startswith("llm")]
    assert llm_rows and all(r["agent_info"]["source"] == "llm" for r in llm_rows)
    assert all(r["agent_info"]["reason"] for r in llm_rows)
    calls = [json.loads(x) for x in open(out / "llm_calls.jsonl", encoding="utf-8")]
    assert len(calls) == len(llm_rows)
    games = [json.loads(x) for x in open(out / "games.jsonl", encoding="utf-8")]
    # 交换先后手：每两局同一种子同一对武将
    by = sorted(games, key=lambda r: r["game"])
    assert by[0]["seed"] == by[1]["seed"] and by[0]["generals"] == by[1]["generals"]
    assert by[0]["seats"] == [0, 1] and by[1]["seats"] == [1, 0]


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
    cfg = C.load(path)
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
                  "output.decisions=false"])
    s = run_config(cfg)
    out = tmp_path / "fake-model"
    assert s["errors"] == 0 and s["sides"]["A"]["llm"]["fallbacks"] == 0
    files = set(os.listdir(out))
    assert "config.yaml" in files and "llm_calls.jsonl" in files and "decisions.jsonl" not in files
    assert "agent: llm" in (out / "config.yaml").read_text(encoding="utf-8")
