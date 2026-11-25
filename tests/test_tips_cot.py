"""武将技巧注入和可选思维链（sgs/agents/llm/addons.py），全用假模型，不联网。"""
import json
import os

import pytest

from sgs.agents.base import decide
from sgs.agents.llm import LLMAgent
from sgs.agents.llm.addons import (TipsCoTPrompt, analysis_len, is_key_decision, parse_cot, three_lines,
                                   to_cot_messages, visible_text, wrap_prompt)
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
    u = msgs[-1]["content"]
    assert "先分析，再给编号" in u and "不超过 80 字" in u and "局面：" in u and "结论：" in u
    assert u.rstrip().endswith('{"action": 0}')                      # 格式示例在最后
    assert "按每道题末尾写的格式回答" in msgs[0]["content"]
    assert "只输出一行 JSON" not in u
    n = len(d.options)
    i, err, ana = p.parse('局面：对面 2 血\n得失：卖血更赚\n结论：不出闪\n{"action": 1}', n)
    assert (i, err) == (1, None) and three_lines(ana) and analysis_len(ana) <= 80
    assert parse_cot("分析：选 0 不好\n1", n)[:2] == (1, None)
    i, err, ana = parse_cot("分析：选 0 和选 1 都行，我再想想", n)
    assert i is None and err and ana
    assert "最后一行" in p.retry(err, n)["content"] and "80 字" in p.retry(err, n)["content"]


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
    assert set(rows) == {"none", "tips", "cot", "both", "reason"}
    assert rows["none"]["cot_decisions"] == 0 and rows["cot"]["cot_decisions"] > 0
    assert rows["tips"]["avg_prompt_tokens"] > rows["none"]["avg_prompt_tokens"]
    md = open(os.path.join(res["dir"], "comparison.md"), encoding="utf-8").read()
    assert "解析失败率" in md and "总 token" in md
    calls = json.load(open(os.path.join(res["dir"], "cot", "games", "0001", "llm_calls.json"), encoding="utf-8"))
    assert any(c.get("analysis") for c in calls)
    assert "分析超长 / 截断" in md
    rc = json.load(open(os.path.join(res["dir"], "reason", "games", "0001", "llm_calls.json"), encoding="utf-8"))
    hit = [c for c in rc if c.get("cot")]
    assert hit and all(c["cot_source"] == "reasoning" and c["analysis"] for c in hit)
    assert all(c["attempts"][-1]["request"].get("reasoning_effort") == "low" for c in rc)


def test_real_experiment_config_loads():
    from sgs import experiment as E
    exp = E.load(os.path.join("configs", "compare-tips-cot.yaml"))
    assert list(exp["_configs"]) == ["none", "tips", "cot", "both"]
    a = exp["_configs"]["both"]["players"]["A"]
    assert a["cot"] == "key" and a["tips"]["self"] is True
    assert exp["_configs"]["none"]["players"]["A"]["cot"] == "off"


# ------------------------------------------------------------------ 分析长度控制
def test_helpers():
    assert three_lines("局面：a\n得失：b\n结论：c")
    assert not three_lines("局面：a\n结论：c")
    assert analysis_len("局面： a b\n得失：c") == 9
    assert visible_text("<think>想很久</think>\n{\"action\": 1}").strip() == '{"action": 1}'
    assert "想" not in visible_text("<think>想到一半被截断")


def test_default_max_tokens_not_capped(srv):
    g = _sell_hp()
    a = LLMAgent(model="fake-cot", base_url=srv.url, prompt="v2", cot="key", max_tokens=4096)
    n0 = len(srv.requests)
    decide(a, g)
    assert srv.requests[n0]["body"]["max_tokens"] == 4096           # 默认不另设上限
    a2 = LLMAgent(model="fake-cot", base_url=srv.url, prompt="v2", cot="key", cot_max_tokens="auto")
    n0 = len(srv.requests)
    decide(a2, g)
    assert srv.requests[n0]["body"]["max_tokens"] == 220            # auto = 80 × 2 + 60


def test_truncated_then_retry(srv):
    g = _sell_hp()
    a = LLMAgent(model="fake-cot-long", base_url=srv.url, prompt="v2", cot="key", cot_max_tokens="auto")
    a.on_game_start({"game_id": "t"})
    rows = []
    a.sink = rows.append
    i = decide(a, g)
    assert 0 <= i < len(g.pending.options)
    assert a.stats["cot_truncated"] == 1 and a.stats["fallbacks"] == 0
    assert rows[-1]["attempts"][0]["truncated"] and rows[-1]["source"] == "llm"


def test_overlong_counted(srv):
    g = _sell_hp()
    a = LLMAgent(model="fake-cot-wordy", base_url=srv.url, prompt="v2", cot="key")
    decide(a, g)
    assert a.stats["cot_overlong"] == 1 and a.last_info["overlong"] and a.last_info["source"] == "llm"


@pytest.mark.parametrize("model", ["fake-reason", "fake-reason-ds"])
def test_reasoning_source(srv, model):
    g = _sell_hp()
    rows = []
    a = LLMAgent(model=model, base_url=srv.url, prompt="v2", cot="key", cot_source="reasoning",
                 reasoning_effort="low", cot_reasoning_effort="high", transcript=rows.append)
    a.on_game_start({"game_id": "t"})
    n0 = len(srv.requests)
    decide(a, g)
    body = srv.requests[n0]["body"]
    assert body["reasoning_effort"] == "high"
    assert "先分析，再给编号" not in body["messages"][-1]["content"]      # 提示词没变，只要编号
    rec = rows[-1]
    assert rec["cot_source"] == "reasoning" and "一共有" in rec["analysis"]
    assert rec["attempts"][0]["reasoning"] == rec["analysis"]
    assert not rec["overlong"] and a.stats["cot_overlong"] == 0
    raw = rec["attempts"][0]["response"]["choices"][0]["message"]
    assert ("reasoning_content" if model.endswith("ds") else "reasoning") in raw   # 原文都留着


def test_bad_cot_source():
    with pytest.raises(ValueError):
        wrap_prompt(make_prompt("v2"), cot="key", cot_source="thinking")


def test_to_cot_messages_rewrites_plain_prompt():
    g = _sell_hp()
    v, d = _view(g), g.pending
    p = wrap_prompt(make_prompt("v2"), tips=True)
    plain = p.messages(v, d)
    sec = p.last_tips
    out = to_cot_messages(plain, "key", 80, drop_tips=sec)
    ref = wrap_prompt(make_prompt("v2"), cot="key").messages(v, d)
    assert out == ref                                             # 去掉技巧后，和直接开 cot: key 的提示词一样
    assert to_cot_messages(ref, "key", 80) == ref                 # 已经是三行格式的不再改


# ------------------------------------------------------------------ 导出
def _play(srv, tmp_path, name, **players):
    from sgs import experiment as E
    exp = E.load(os.path.join("configs", "compare-tips-cot-fake.yaml"),
                 sets=[f"base.players.A.base_url={srv.url}", f"dir={tmp_path}/{name}", "base.match.games=2"]
                 + [f"base.players.A.{k}={v}" for k, v in players.items()])
    return E.run(exp)["dir"]


def _export(d, tmp_path, srv, **over):
    from sgs_data import cot_export as X
    cfg = X.load(data={"runs": [d], "out": str(tmp_path / ("out-" + "-".join(f"{k}{v}" for k, v in over.items()))),
                       "compressor": {"agent": "llm", "base_url": srv.url, "model": "fake-cot"}, **over})
    return X.run(cfg)


def test_export_drop_and_compress(srv, tmp_path):
    d = _play(srv, tmp_path, "wordy", model="fake-cot-wordy")
    res = _export(os.path.join(d, "cot"), tmp_path, srv)
    assert res["kept"] == 0 and res["counts"].get("dropped_overlong", 0) > 0     # 默认：超长丢掉
    res = _export(os.path.join(d, "cot"), tmp_path, srv, overlong="compress")
    assert res["kept"] > 0 and res["counts"].get("compressed", 0) == res["kept"]
    rows = [json.loads(x) for x in open(os.path.join(res["dir"], "sft.jsonl"), encoding="utf-8")]
    ans = rows[0]["messages"][-1]["content"]
    ana, tail = ans.rsplit("\n", 1)
    assert three_lines(ana) and analysis_len(ana) <= 80 and json.loads(tail)["action"] >= 0
    assert "先分析，再给编号" in rows[0]["messages"][-2]["content"]
    assert os.path.exists(os.path.join(res["dir"], "stats.md"))


def test_export_from_reasoning(srv, tmp_path):
    d = _play(srv, tmp_path, "reason")
    res = _export(os.path.join(d, "reason"), tmp_path, srv, drop_tips=True)
    assert res["kept"] > 0 and res["counts"].get("compressed") == res["kept"]
    rows = [json.loads(x) for x in open(os.path.join(res["dir"], "sft.jsonl"), encoding="utf-8")]
    for r in rows:
        text = json.dumps(r["messages"], ensure_ascii=False)
        assert "一共有" not in text                                 # 思考原文不进训练数据
        assert "实战技巧" not in r["messages"][0]["content"]         # drop_tips
        assert "先分析，再给编号" in r["messages"][-2]["content"]   # 提示词换成三行格式
    # 压缩模型压不好就丢掉
    from sgs_data import cot_export as X
    cfg = X.load(data={"runs": [os.path.join(d, "reason")], "out": str(tmp_path / "bad"),
                       "compressor": {"agent": "llm", "base_url": srv.url, "model": "fake-cot-wordy"}})
    res = X.run(cfg)
    assert res["kept"] == 0 and res["counts"].get("compress_failed", 0) > 0


def test_export_needs_compressor_for_reasoning(srv, tmp_path):
    from sgs import config as C
    from sgs_data import cot_export as X
    d = _play(srv, tmp_path, "reason2")
    cfg = X.load(data={"runs": [os.path.join(d, "reason")], "out": str(tmp_path / "x")})
    with pytest.raises(C.ConfigError):
        X.run(cfg)


def test_export_config_loads():
    from sgs_data import cot_export as X
    cfg = X.load(os.path.join("configs", "data", "cot-export.yaml"))
    assert cfg["overlong"] == "drop" and cfg["max_chars"] == 80 and cfg["analysis_from"] == "auto"
