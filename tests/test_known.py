"""已知手牌：牌通过公开途径进入手牌时，相关的人知道它在谁手里；离开手牌时删除。严格按视角隔离。"""
import random

import pytest

from sgs.core.view import HiddenCardError
from sgs.testing import blank_game, choose, equip, give, labels, stack_deck


def names(g, cids):
    return [g.cards[c].name for c in cids]


def known(g, viewer, seat):
    """viewer 视角下 seat 手里已知的牌名。"""
    return names(g, g.view(viewer).players[seat].known_hand)


def known_by(g, seat):
    """seat 视角下：别人知道的我的手牌 {座位: 牌名}。"""
    return {s: names(g, v) for s, v in (g.view(seat).me.known_by or {}).items()}


def test_drawn_cards_are_private():
    g = blank_game(["关羽", "孙权"])
    give(g, 1, "♠8杀")
    g.draw(1, 2)
    assert known(g, 0, 1) == [] and known_by(g, 1) == {}
    v = g.view(0)
    with pytest.raises(HiddenCardError):
        v.card(g.players[1].hand[0])
    assert "known_hand" in v.to_dict()["players"][1] and v.to_dict()["players"][1]["known_hand"] == []


def test_amazing_grace_cards_are_known_until_they_leave():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "五谷丰登")
    stack_deck(g, "♠7杀", "♥3桃")
    choose(g, "五谷丰登")
    choose(g, "桃")
    assert known(g, 0, 1) == ["杀"] and known(g, 1, 0) == ["桃"]
    assert known_by(g, 1) == {0: ["杀"]} and known_by(g, 0) == {1: ["桃"]}
    info = g.view(0).known_info[g.players[1].hand[0]]
    assert info["how"] == "五谷丰登" and info["turn"] == 1
    # 孙权之后摸的牌不算已知
    g.draw(1, 2)
    assert known(g, 0, 1) == ["杀"] and g.view(0).players[1].hand_count == 3
    # 关羽用掉桃（回复不了也没关系：直接弃掉），离开手牌后不再已知
    peach = g.players[0].hand[0]
    g.to_discard([peach])
    assert known(g, 1, 0) == [] and known_by(g, 0) == {} and peach not in g.known
    to = g.view(0).to_dict()["players"][1]["known_hand"]
    assert to[0]["name"] == "杀" and to[0]["how"] == "五谷丰登"


def test_snatch_from_hand_both_know():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠3顺手牵羊")
    give(g, 1, "♥3桃")
    choose(g, "顺手牵羊")
    pass_nullify(g)
    if g.pending and g.pending.player == 0 and "顺手牵羊" in g.pending.prompt:
        g.act(0)
    assert names(g, g.players[0].hand) == ["桃"]
    assert known(g, 1, 0) == ["桃"] and known_by(g, 0) == {1: ["桃"]}
    k = g.known[g.players[0].hand[0]]
    assert k["how"] == "顺手牵羊" and k["zone"] == "h" and k["from"] == 1


def pass_nullify(g):
    while g.pending and "无懈可击" in g.pending.prompt:
        choose(g, "不")


def test_snatch_equip_and_collateral_weapon_are_known():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠3顺手牵羊")
    equip(g, 1, "♠2八卦阵")
    choose(g, "顺手牵羊")
    pass_nullify(g)
    if g.pending and g.pending.player == 0 and "顺手牵羊" in g.pending.prompt:
        choose(g, "八卦阵")
    assert known(g, 1, 0) == ["八卦阵"] and g.known[g.players[0].hand[0]]["zone"] == "e"

    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♣Q借刀杀人")
    equip(g, 1, "青釭剑")
    choose(g, "借刀杀人")
    assert known(g, 1, 0) == ["青釭剑"] and g.known[g.players[0].hand[0]]["how"] == "借刀杀人"


def test_rende_given_cards_are_known_to_receiver_and_giver():
    g = blank_game(["刘备", "孙权"])
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "仁德")
    choose(g, "♠7杀")
    choose(g, "♠8杀")
    assert known(g, 0, 1) == ["杀", "杀"] and known_by(g, 1) == {0: ["杀", "杀"]}


def test_fanjian_card_is_public():
    g = blank_game(["周瑜", "关羽"])
    choose(g, "否")
    give(g, 0, "♠7杀")
    choose(g, "反间")
    choose(g, "♥")
    assert known(g, 0, 1) == ["杀"] and g.known[g.players[1].hand[0]]["how"] == "反间"


def test_jianxiong_card_is_known():
    g = blank_game(["关羽", "曹操"])
    give(g, 0, "♠7杀")
    choose(g, "♠7杀")
    choose(g, "是，发动【奸雄】")
    assert known(g, 0, 1) == ["杀"]


def test_tuxi_taken_cards_are_known():
    g = blank_game(["孙权", "张辽"])
    give(g, 0, "♠7杀", "♥3桃")
    choose(g, "结束出牌阶段")
    choose(g, "是，发动【突袭】")
    while g.pending and g.pending.player == 1 and "突袭" in g.pending.prompt:
        opts = labels(g)
        i = next((i for i, o in enumerate(opts) if "孙权" in o), None)
        if i is None:
            break
        g.act(i)
    took = [c for c in g.players[1].hand if c in g.known]
    assert took and all(g.known[c]["how"] == "突袭" for c in took)
    assert sorted(known(g, 0, 1)) == sorted(names(g, took))


def test_determinize_keeps_known_cards():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "五谷丰登")
    stack_deck(g, "♠7杀", "♥3桃")
    choose(g, "五谷丰登")
    choose(g, "桃")
    g.draw(1, 3)
    kept = g.players[1].hand[0]
    for s in range(10):
        d = g.determinize(0, random.Random(s))
        assert kept in d.players[1].hand and len(d.players[1].hand) == 4
        assert d.known_by(0, 1) == [kept]


def test_privileged_observation_lists_who_knows():
    g = blank_game(["刘备", "孙权"])
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "仁德")
    choose(g, "♠7杀")
    choose(g, "♠8杀")
    obs = g.observe(None)
    assert sorted(obs["players"][1]["known_by"].values()) == [[0, 1], [0, 1]]


def test_known_invariants_in_random_games():
    from sgs import Game
    from sgs.agents.base import RandomAgent, decide
    from sgs.core.registry import GENERALS, load_all
    load_all()
    gens = sorted(GENERALS)
    seen = 0
    for seed in range(40):
        rng = random.Random(seed)
        g = Game("duel", seed=seed, generals=rng.sample(gens, 2), auto_single=False)
        ag = [RandomAgent(seed), RandomAgent(seed + 1)]
        while not g.over and g.steps < 3000:
            g.act(decide(ag[g.pending.player], g))
            hands = {c: p.seat for p in g.players for c in p.hand}
            for c, k in g.known.items():
                assert c in hands and hands[c] in k["by"] and len(k["by"]) > 1
            for v in range(2):
                view = g.view(v)
                for p in view.players:
                    if p.seat != v:
                        assert set(p.known_hand) <= set(g.players[p.seat].hand)
                        seen += len(p.known_hand)
    assert seen > 0


# ------------------------------------------------------------------ 新提示词 v2
from sgs.agents.llm.prompt import FLOW_KINDS, make_prompt  # noqa: E402


def _grace_game():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "五谷丰登")
    stack_deck(g, "♠7杀", "♥3桃")
    choose(g, "五谷丰登")
    choose(g, "桃")
    g.draw(1, 2)
    return g


def test_v2_prompt_shows_known_cards_both_ways():
    g = _grace_game()
    v = g.view(0, with_logs=True, derived=True)
    txt = make_prompt("v2").messages(v, g.pending)[1]["content"]
    assert "手牌 3 张，其中你知道的：♠7杀（第 1 回合五谷丰登，亮出过）" in txt
    assert "对手知道你手里有：♥3桃（第 1 回合五谷丰登，亮出过）" in txt
    # 孙权后来摸的两张牌不出现
    for c in g.players[1].hand[1:]:
        assert str(g.cards[c]) not in txt or any(str(g.cards[x]) == str(g.cards[c]) for x in v._cards)
    old = make_prompt("default").messages(v, g.pending)[1]["content"]
    assert "其中你知道的" not in old and "手牌数：3" in old


def test_v2_recent_events_only_two_turns_without_flow():
    from sgs import Game
    from sgs.agents import RuleAgent, decide
    for seed in range(50):
        g = Game("duel", seed=seed, generals=["张飞", "黄盖"])
        ag = [RuleAgent(1), RuleAgent(2)]
        while g.turn_count < 6 and not g.over:
            g.act(decide(ag[g.pending.player], g))
        if not g.over:
            break
    v = g.view(g.pending.player, with_logs=True, derived=True)
    pb = make_prompt("v2")
    h = pb.history(v)
    assert h.startswith("【最近发生的事】") and f"第 {v.turn - 1} 回合（" in h and f"第 {v.turn - 2} 回合" not in h
    flow = [e["text"] for e in v.log_entries if e["kind"] in FLOW_KINDS and e["turn"] >= v.turn - 1]
    assert flow and not any(t in h for t in flow if "阶段" in t)
    keep = make_prompt("v2", keep_flow=True).history(v)
    assert any(t in keep for t in flow if "阶段" in t)
    # recent_turns=0 退回旧方式
    assert make_prompt("v2", recent_turns=0).history(v) == make_prompt("default").history(v)


def test_v2_never_leaks_hidden_cards():
    from sgs import Game
    from sgs.agents import RandomAgent, decide
    pb = make_prompt("v2")
    checked = 0
    for seed in range(15):
        g = Game("duel", seed=seed, generals=["刘备", "孙权"] if seed % 2 else ["甄姬", "张辽"])
        ag = [RandomAgent(seed), RandomAgent(seed + 7)]
        while not g.over:
            d = g.pending
            if checked < 400 and len(d.options) > 1:
                v = g.view(d.player, with_logs=True, derived=True)
                txt = "\n".join(m["content"] for m in pb.messages(v, d))
                shown = {str(g.cards[c]) for c in v._cards}
                opp = g.players[1 - d.player]
                for c in opp.hand:
                    if c not in v._cards and str(g.cards[c]) not in shown:
                        assert str(g.cards[c]) not in txt
                checked += 1
            g.act(decide(ag[d.player], g))
    assert checked > 100


def test_prompt_args_from_config_and_template_in_records(tmp_path):
    from sgs.agents import make_agent
    ag = make_agent({"kind": "llm", "model": "m", "base_url": "http://127.0.0.1:9/v1", "prompt": "v2",
                     "prompt_args": {"recent_turns": 3, "keep_flow": True}}, seed=0)
    assert ag.prompt.recent_turns == 3 and ag.prompt.keep_flow and ag.prompt_name == "v2"
    ag.close()


def test_forced_selection_is_auto():
    from sgs.agents import auto_reason
    g = blank_game(["关羽", "夏侯惇"])
    give(g, 0, "♠7杀", "♥3桃", "♦2闪")
    stack_deck(g, "♠9杀")
    choose(g, "♠7杀")
    choose(g, "是，发动【刚烈】")
    choose(g, "弃置两张手牌")
    assert g.pending.ctx.get("forced") and auto_reason(g.pending) == "剩下的牌都要选"
    assert len(g.pending.options) == 2
