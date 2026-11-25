"""造数据包 sgs_data：关键局面、分组、观星补丁、打标签、配比、整条流水线、裁判、题库评测（假模型，不联网）。"""
import json
import os
import random

import pytest

from sgs.core.registry import load_all
from sgs.tools.fake_llm import FakeLLMServer

from sgs_data import pipeline
from sgs_data.board import Board, use_card
from sgs_data.evalbank import load as eb_load, run as eb_run
from sgs_data.groups import group_options
from sgs_data.label import determinize, evaluate, guanxing_known, make_policy
from sgs_data.mix import cap_tags
from sgs_data.scenarios import all_scenarios, make_scenario

load_all()


@pytest.fixture(scope="module")
def srv():
    with FakeLLMServer(slow=0.5) as s:
        yield s


@pytest.mark.parametrize("name", sorted(all_scenarios()))
def test_scenarios_build_and_replay(name):
    sc = make_scenario(name)
    for seed in range(5):
        g, variant, used = sc.build_retry(seed)
        d = g.pending
        assert d.player == sc.hero_seat
        groups, labels = group_options(g, d)
        assert len(groups) >= 2
        # 同一个种子摆出来完全一样
        g2, v2, used2 = sc.build_retry(seed)
        assert used2 == used
        assert v2 == variant
        assert [o.key for o in g2.pending.options] == [o.key for o in d.options]
        assert g2.players[0].hand == g.players[0].hand and g2.deck == g.deck
        # 配方能重放
        g3 = pipeline.rebuild({"type": "scenario", "name": name, "params": {}, "seed": used})
        assert [o.key for o in g3.pending.options] == [o.key for o in d.options]


def test_groups_merge_same_color_cards():
    g, _, _ = make_scenario("zhenji_discard").build_retry(3)
    groups, labels = group_options(g, g.pending)
    assert len(labels) == len(set(labels))  # 合并后没有重复的描述
    assert sum(len(x) for x in groups) == len(g.pending.options)


def test_guanxing_known_and_determinize_keep_top():
    b = Board(["诸葛亮", "曹操"], seed=1)
    g = b.g
    g.logging = True
    # 打到 0 号位下一回合的观星
    from sgs_data.board import end_play
    b.to_play_phase(0)
    b.until(lambda gg: gg.pending.player == 0 and gg.pending.ctx.get("skill") == "观星"
            and gg.pending.kind == "choose", scripts=[end_play(0)], limit=400)
    # 1v1 观星只看 2 张，放顶上的马上就被自己摸走；放到底下的才会一直知道。这里全放底下
    while g.pending.ctx.get("skill") == "观星":
        i = next(j for j, o in enumerate(g.pending.options) if o.key[0] == "bottom")
        g.act(i)
    known = guanxing_known(g, 0)
    assert len(known["bottom"]) == 2
    assert list(reversed(g.deck[:2])) == known["bottom"]
    rng = random.Random(0)
    for _ in range(5):
        w = determinize(g, 0, rng, True, known)
        assert list(reversed(w.deck[:2])) == known["bottom"]
        for c in known["bottom"]:
            assert c not in w.players[1].hand
        assert sorted(w.deck + w.players[1].hand) == sorted(g.deck + g.players[1].hand)


def test_evaluate_finds_clear_answer():
    # 司马懿反馈，对手只剩一张杀在手里（张飞咆哮）：拿手牌明显好
    sc = make_scenario("simayi_fankui")
    g, _, _ = sc.build_retry(0)
    ev = evaluate(g, make_policy("rule+"), {"n_min": 16, "n_max": 32}, seed=0)
    assert ev["status"] in ("confident", "tie")
    assert len(ev["values"]) == len(ev["groups"]) >= 2
    assert ev["best"] in ev["acceptable"]
    assert all(j not in ev["acceptable"] for j in ev["worse"])


def test_cap_tags_keeps_counterexamples():
    items = [{"tag": "A", "id": i} for i in range(9)] + [{"tag": "B", "id": 100}]
    out = cap_tags(items, 0.7, random.Random(0))
    tags = [x["tag"] for x in out]
    assert tags.count("B") == 1
    assert tags.count("A") / len(tags) <= 0.7 + 1e-9


def _small_cfg(tmp_path, **extra):
    data = {"name": "t", "workers": 2, "out": str(tmp_path / "out"),
            "rollout": {"policy": "rule+", "n_min": 8, "n_max": 16},
            "key": {"scenarios": {"simayi_fankui": 3, "zhenji_discard": 2}, "test_per_scenario": 2,
                    "test_rollout": {"n_min": 8, "n_max": 16}},
            "normal": {"games": 2, "per_game": 2, "rollout": {"n_min": 8, "n_max": 8}},
            "progress": {"every": 0}}
    for k, v in extra.items():
        data[k] = v
    return pipeline.load(None, data=data)


def test_pipeline_small_run(tmp_path, capsys):
    cfg = _small_cfg(tmp_path)
    res = pipeline.run(cfg)
    top = res["dir"]
    for f in ("sft.jsonl", "dpo.jsonl", "test.jsonl", "review.md", "stats.md", "config.yaml",
              "items/key-train.jsonl", "items/key-test.jsonl", "items/normal.jsonl"):
        assert os.path.exists(os.path.join(top, f)), f
    items = [json.loads(x) for x in open(os.path.join(top, "items/key-train.jsonl"), encoding="utf-8")]
    assert len(items) == 5
    for it in items:
        if it.get("failed"):
            continue
        assert it["messages"][0]["role"] == "system"
        assert "合法动作" in it["messages"][-1]["content"]
    for line in open(os.path.join(top, "sft.jsonl"), encoding="utf-8"):
        row = json.loads(line)
        assert row["messages"][-1]["role"] == "assistant"
        k = json.loads(row["messages"][-1]["content"])["action"]
        assert isinstance(k, int)
    for line in open(os.path.join(top, "dpo.jsonl"), encoding="utf-8"):
        row = json.loads(line)
        assert row["chosen"] != row["rejected"] and row["margin"] > 0
    err = capsys.readouterr().err
    assert "关键局面（训练）" in err and "普通局面" in err


def test_pipeline_with_fake_judge_and_evalbank(tmp_path, srv):
    judge = {"enabled": True, "player": {"agent": "llm", "model": "fake", "base_url": srv.url}, "votes": 3,
             "scope": "all", "concurrency": 2}
    cfg = _small_cfg(tmp_path, judge=judge, normal={"enabled": False})
    res = pipeline.run(cfg)
    items = [json.loads(x) for x in open(os.path.join(res["dir"], "items/key-train.jsonl"), encoding="utf-8")]
    judged = [it for it in items if it.get("judge")]
    assert judged and all(it["judge"]["votes"] == 3 for it in judged)
    bank = os.path.join(res["dir"], "items/key-test.jsonl")
    ecfg = eb_load(None, data={"bank": bank, "player": "rule+", "out": str(tmp_path / "eb")})
    out = eb_run(ecfg)
    assert len(out["results"]) == 4
    assert "题库评测" in out["report"]


def test_load_rejects_unknown_scenario():
    from sgs import config as C
    with pytest.raises(C.ConfigError):
        pipeline.load(None, data={"key": {"scenarios": {"nope": 3}}})


@pytest.mark.parametrize("name", ["demo", "full"])
def test_example_configs_load(name):
    cfg = pipeline.load(os.path.join("configs", "data", f"{name}.yaml"))
    assert set(cfg["key"]["scenarios"]) <= set(all_scenarios())


def test_evalbank_config_loads(tmp_path):
    bank = tmp_path / "t.jsonl"
    bank.write_text("", encoding="utf-8")
    cfg = eb_load(os.path.join("configs", "data", "evalbank.yaml"), sets=[f"bank={bank}"])
    assert cfg["player"] == {"include": "../models/qwen-local.yaml"}
