"""武将技巧注入和可选思维链（sgs/agents/llm/addons.py），全用假模型，不联网。"""
import json
import os

import pytest

from sgs.agents.base import decide
from sgs.agents.llm import LLMAgent
from sgs.agents.llm.addons import TipsCoTPrompt, is_key_decision, parse_cot, wrap_prompt
from sgs.agents.llm.prompt import make_prompt
from sgs.core.registry import load_all
from sgs.tools.fake_llm import FakeLLMServer

from sgs_data.board import Board
from sgs_data.scenarios import make_scenario

load_all()


@pytest.fixture(scope="module")
def srv():
    with FakeLLMServer() as s:
        yield s


def _view(g):
    return g.view(g.pending.player, with_logs=True, derived=True)


def _sell_hp():
    g, _, _ = make_scenario("guojia_sell_hp").build_retry(0)
    return g


def test_off_is_unchanged():
    g = _sell_hp()
    plain = make_prompt("v2")
    assert wrap_prompt(plain, tips=False, cot="off") is plain
    assert wrap_prompt(plain, tips=None, cot=False) is plain      # YAML 里没加引号的 off 是 False
    a = LLMAgent(model="x", base_url="http://127.0.0.1:9/v1", prompt="v2")
    assert type(a.prompt) is type(plain)
    assert a.prompt.messages(_view(g), g.pending) == plain.messages(_view(g), g.pending)


def test_tips_injected_for_own_general_only():
    g = _sell_hp()
    v, d = _view(g), g.pending
    me = v.players[v.viewer].general
    assert me == "郭嘉"
    p = wrap_prompt(make_prompt("v2"), tips=True)
    sys_ = p.messages(v, d)[0]["content"]
    assert "郭嘉 的实战技巧" in sys_ and "只是参考" in sys_ and "草稿" not in sys_   # 注释不进提示词
    assert sys_.rstrip().endswith('{"action": 编号}')                               # 输出格式还在最后
    assert "对手武将" not in sys_
    opp = v.players[1 - v.viewer].general
    p2 = wrap_prompt(make_prompt("v2"), tips={"opponent": True})
    s2 = p2.messages(v, d)[0]["content"]
    has_file = os.path.exists(os.path.join("configs", "tips", f"{opp}.md"))
    assert ("对手武将" in s2) == has_file
    # 用户消息没变
    assert p.messages(v, d)[1] == make_prompt("v2").messages(v, d)[1]


def test_tips_missing_file_adds_nothing(tmp_path):
    g = _sell_hp()
    p = wrap_prompt(make_prompt("v2"), tips={"dir": str(tmp_path)})
    assert p.messages(_view(g), g.pending) == make_prompt("v2").messages(_view(g), g.pending)


@pytest.mark.parametrize("name,key", [("guojia_sell_hp", True), ("zhenji_discard", True),
                                      ("simayi_fankui", True), ("guojia_yiji_kongcheng", True),
                                      ("zhuge_kongcheng", False)])
def test_key_decisions(name, key):
    g, _, _ = make_scenario(name).build_retry(0)
    assert is_key_decision(_view(g), g.pending) is key


def test_plain_dodge_is_not_key():
    # 没有卖血技能的武将被杀，出不出闪不算关键决策
    from sgs_data.board import use_card
    b = Board(["张飞", "关羽"], seed=3)
    b.to_play_phase(0)
    b.clear(0)
    b.clear(1)
    b.give(1, "闪")
    b.give(0, "杀")
    g = b.finish()
    b.until(lambda g: g.pending.player == 1 and g.pending.kind == "respond"
            and (g.pending.ctx or {}).get("slash"), [use_card(0, "杀", 1)], limit=60)
    assert is_key_decision(_view(g), g.pending) is False


def test_cot_prompt_and_parse():
    g = _sell_hp()
    v, d = _view(g), g.pending
    p = wrap_prompt(make_prompt("v2"), cot="key")
    msgs = p.messages(v, d)
    assert p.last_cot
    assert msgs[-1]["content"].endswith('最后一行只写 JSON：{"action": 编号}')
    assert "按每道题末尾写的格式回答" in msgs[0]["content"]
    assert "只输出一行 JSON" not in msgs[-1]["content"]
    n = len(d.options)
    i, err, ana = p.parse('分析：选 0 不好，卖血更赚。\n{"action": 1}', n)
    assert (i, err) == (1, None) and ana == "选 0 不好，卖血更赚。"
    assert parse_cot("分析：选 0 不好\n1", n)[:2] == (1, None)
    i, err, ana = parse_cot("分析：选 0 和选 1 都行，我再想想", n)
    assert i is None and err and ana
    assert "最后一行" in p.retry(err, n)["content"]


def test_cot_off_for_non_key_in_key_mode():
    g, _, _ = make_scenario("zhuge_kongcheng").build_retry(0)
    v, d = _view(g), g.pending
    p = wrap_prompt(make_prompt("v2"), cot="key")
    msgs = p.messages(v, d)
    assert not p.last_cot
    assert msgs[1] == make_prompt("v2").messages(v, d)[1]
    p_all = wrap_prompt(make_prompt("v2"), cot="all")
    p_all.messages(v, d)
    assert p_all.last_cot


def test_agent_records_analysis_and_counts_failures(srv, tmp_path):
    rows = []
    g = _sell_hp()
    a = LLMAgent(model="fake-cot-bad", base_url=srv.url, prompt="v2", tips=True, cot="key", transcript=rows.append)
    a.on_game_start({"game_id": "t"})
    i = decide(a, g)
    assert 0 <= i < len(g.pending.options)
    assert a.stats["cot_decisions"] == 1 and a.stats["illegal"] == 1 and a.stats["fallbacks"] == 0
    rec = rows[-1]
    assert rec["cot"] and rec["analysis"] and rec["attempts"][0]["error_kind"] == "illegal"
    assert rec["addons"]["cot"] == "key"
    assert a.last_info["analysis"] == rec["analysis"]


def test_bad_cot_value():
    with pytest.raises(ValueError):
        wrap_prompt(make_prompt("v2"), cot="sometimes")


def test_experiment_runs_four_groups(srv, tmp_path):
    from sgs import experiment as E
    exp = E.load(os.path.join("configs", "compare-tips-cot-fake.yaml"),
                 sets=[f"base.players.A.base_url={srv.url}", f"dir={tmp_path}/exp"])
    res = E.run(exp)
    rows = {r["variant"]: r for r in res["rows"]}
    assert set(rows) == {"none", "tips", "cot", "both"}
    assert rows["none"]["cot_decisions"] == 0 and rows["cot"]["cot_decisions"] > 0
    assert rows["tips"]["avg_prompt_tokens"] > rows["none"]["avg_prompt_tokens"]
    md = open(os.path.join(res["dir"], "comparison.md"), encoding="utf-8").read()
    assert "解析失败率" in md and "总 token" in md
    calls = json.load(open(os.path.join(res["dir"], "cot", "games", "0001", "llm_calls.json"), encoding="utf-8"))
    assert any(c.get("analysis") for c in calls)


def test_real_experiment_config_loads():
    from sgs import experiment as E
    exp = E.load(os.path.join("configs", "compare-tips-cot.yaml"))
    assert list(exp["_configs"]) == ["none", "tips", "cot", "both"]
    a = exp["_configs"]["both"]["players"]["A"]
    assert a["cot"] == "key" and a["tips"]["self"] is True
    assert exp["_configs"]["none"]["players"]["A"]["cot"] == "off"
