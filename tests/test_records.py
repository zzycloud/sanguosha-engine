"""视角隔离、完整记录、导出和录像的测试。"""
import json

import pytest

from sgs import Game
from sgs.agents import Agent, RandomAgent, RuleAgent, decide, play_game
from sgs.core.game import log_text
from sgs.core.view import HiddenCardError, PlayerView
from sgs.export import card_table, play_and_export
from sgs.replay import Recorder, record, record_new
from sgs.testing import blank_game, choose, give


# ------------------------------------------------------------------ 视角隔离
def test_view_hides_opponent_cards():
    g = Game(seed=3, generals=["关羽", "孙权"])
    v = g.view(0)
    for c in g.players[0].hand:
        assert v.card(c).id == c
    for c in g.players[1].hand:
        assert not v.can_see(c)
        with pytest.raises(HiddenCardError):
            v.card(c)
    for c in g.deck:
        assert not v.can_see(c)
    assert v.players[1].hand is None and v.players[1].hand_count == len(g.players[1].hand)


def test_view_holds_no_game_reference():
    g = Game(seed=3, generals=["关羽", "孙权"])
    v = g.view(1)
    seen = set()

    def walk(x, depth=0):
        assert not isinstance(x, Game)
        if depth > 4 or id(x) in seen:
            return
        seen.add(id(x))
        if isinstance(x, dict):
            for k, val in x.items():
                walk(k, depth + 1)
                walk(val, depth + 1)
        elif isinstance(x, (list, tuple, set)):
            for val in x:
                walk(val, depth + 1)
        elif hasattr(x, "__dict__"):
            walk(vars(x), depth + 1)
        elif hasattr(x, "__slots__"):
            for k in x.__slots__:
                walk(getattr(x, k, None), depth + 1)

    walk(v)


def test_agents_only_get_view():
    got = []

    class Spy(Agent):
        def choose(self, view, d):
            got.append(type(view))
            return 0

    g = Game(seed=1, generals=["关羽", "吕布"])
    play_game(g, [Spy(), Spy()], max_steps=50)
    assert got and all(t is PlayerView for t in got)


def test_view_shows_decision_cards():
    # 观星看到的牌堆顶对决策者可见
    g = blank_game(["诸葛亮", "吕布"])
    v = g.view(0)
    assert all(v.can_see(c) for c in g.players[0].hand)


@pytest.mark.parametrize("seed", range(40))
def test_rule_agent_never_peeks(seed):
    # 规则 AI 只能用视角接口；看不见的牌会抛 HiddenCardError
    g = Game(seed=seed, pick="random")
    play_game(g, [RuleAgent(seed), RuleAgent(seed + 1)])
    assert g.over


def test_player_observation_has_no_waiting_hint():
    g = Game(seed=3, generals=["关羽", "孙权"])
    other = 1 - g.pending.player
    obs = g.observe(other)
    assert "decision" not in obs and "waiting_for" not in obs


# ------------------------------------------------------------------ 完整记录
def test_no_dodge_is_logged():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀")
    choose(g, "♠7杀")
    texts = g.visible_logs(None)
    i = next(k for k, t in enumerate(texts) if "没有使用闪" in t)
    j = next(k for k, t in enumerate(texts) if "造成 1 点伤害" in t)
    assert i < j
    assert "没有可用的牌" in texts[i]
    # 对手看到的是公开文本，不知道是没有闪还是不想出
    assert any(t == "孙权[1] 没有使用闪" for t in g.visible_logs(0))


def test_declined_dodge_is_logged():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀")
    give(g, 1, "♦2闪")
    choose(g, "♠7杀")
    choose(g, "不出")
    assert any("没有使用闪：选择不出" in t for t in g.visible_logs(1))
    assert any(t == "孙权[1] 没有使用闪" for t in g.visible_logs(0))


def test_auto_decisions_are_recorded():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀")
    choose(g, "♠7杀")
    autos = [d for d in g.decisions if d["auto"]]
    assert any(d["player"] == 1 and d["kind"] == "respond" and d["options"][0]["label"] == "不出" for d in autos)
    manual = [d for d in g.decisions if not d["auto"]]
    assert len(manual) == len(g.history)


def test_nullify_result_logged():
    g = blank_game(["关羽", "张飞"])
    give(g, 0, "无中生有")
    give(g, 1, "无懈可击")
    choose(g, "使用 ♥")
    choose(g, "使用 ")
    assert any("无懈可击结算完毕" in t and "被抵消" in t for t in g.visible_logs(None))


def test_phase_and_turn_end_logged():
    g = Game(seed=2, generals=["关羽", "吕布"])
    play_game(g, [RuleAgent(1), RuleAgent(2)])
    kinds = {e["kind"] for e in g.logs}
    assert {"phase", "pass", "turn"} <= kinds
    assert any("回合结束" in e["text"] for e in g.logs)


def test_private_only_log_hidden_from_others():
    e = {"text": "", "private": {1: "x 没有使用无懈可击"}}
    assert log_text(e, 0) is None and log_text(e, 1) == "x 没有使用无懈可击"
    assert log_text(e, None) == "x 没有使用无懈可击"


# ------------------------------------------------------------------ 导出
def test_export_rows():
    rows = play_and_export(5, agents=["rule", "random"], generals=["关羽", "吕布"])
    assert rows
    r0 = rows[0]
    for k in ("game_id", "seed", "step", "player", "obs", "history", "legal_actions", "action",
              "action_key", "action_label", "privileged", "outcome"):
        assert k in r0, k
    json.dumps(rows, ensure_ascii=False)
    winner = r0["outcome"]["winner"]
    for r in rows:
        seat = r["player"]
        assert 0 <= r["action"] < len(r["legal_actions"])
        assert r["action_label"] == r["legal_actions"][r["action"]]["label"]
        opp = r["obs"]["players"][1 - seat]
        assert "hand" not in opp
        assert "hand" in r["obs"]["players"][seat]
        # obs 里不出现对手手牌的 id
        hidden = set(r["privileged"]["hands"][str(1 - seat)])
        shown = {c["id"] for p in r["obs"]["players"] for z in ("equips",) for c in p[z].values()}
        shown |= {c["id"] for c in r["obs"]["players"][seat]["hand"]}
        assert not (hidden & shown)
        if winner is not None:
            assert r["outcome"]["reward"] == (1.0 if winner == seat else -1.0)


def test_export_history_concatenates_to_full_view():
    rows = play_and_export(9, agents=["rule", "rule"], generals=["张辽", "黄盖"])
    full = play_and_export(9, agents=["rule", "rule"], generals=["张辽", "黄盖"], history="full")
    for seat in (0, 1):
        mine = [r for r in rows if r["player"] == seat]
        mine_full = [r for r in full if r["player"] == seat]
        acc = []
        for r, f in zip(mine, mine_full):
            acc += r["history"]
            assert acc == f["history"]


def test_export_is_deterministic():
    a = play_and_export(11, agents=["rule", "random"])
    b = play_and_export(11, agents=["rule", "random"])
    assert [r["action"] for r in a] == [r["action"] for r in b]


def test_export_include_auto_and_rollout():
    rows = play_and_export(3, agents=["rule", "rule"], generals=["关羽", "吕布"], include_auto=True,
                           rollouts=1, privileged=False)
    assert any(r["auto"] for r in rows)
    steps = [r["step"] for r in rows]
    assert steps == sorted(steps) and len(set(steps)) == len(steps)
    assert all("privileged" not in r for r in rows)
    labeled = [r for r in rows if "rollout_values" in r]
    assert labeled and all(len(r["rollout_values"]) == len(r["legal_actions"]) for r in labeled)


def test_card_table_matches_ids():
    t = card_table()
    g = Game(seed=4, generals=["关羽", "吕布"])
    assert len(t) == len(g.cards)
    assert all(t[c.id]["name"] == c.name for c in g.cards)


# ------------------------------------------------------------------ 录像
def test_replay_includes_single_option_steps():
    g = Game(seed=1, generals=["关羽", "吕布"], auto_single=False)
    rep = record(g, [RuleAgent(1), RuleAgent(2)])
    assert rep["format"] == "sgs-replay/3"
    assert any(s["auto"] for s in rep["steps"])
    assert rep["steps"][-1]["type"] == "end" and rep["steps"][-1]["decision"] is None
    assert "武圣" in rep["meta"]["skills"] and rep["meta"]["cards"]["杀"]["type"] == "basic"
    n_logs = sum(len(s["logs"]) for s in rep["steps"])
    assert n_logs == len(g.logs)


def test_replay_one_event_per_step():
    rep = record_new([RuleAgent(1), RuleAgent(2)], "duel", seed=1, generals=["关羽", "貂蝉"])
    events = [s for s in rep["steps"] if s["type"] == "event"]
    assert events and all(len(s["logs"]) == 1 for s in events)
    assert all(not s["logs"] for s in rep["steps"] if s["type"] != "event")
    # 开局的日志也各自一步：两条武将信息、两次起始手牌
    assert [s["logs"][0]["kind"] for s in rep["steps"][:4]] == ["setup", "setup", "draw", "draw"]
    # 状态是事件发生之后的：摸牌那一步手牌数已经变了
    for k, s in enumerate(rep["steps"][1:], 1):
        if s["type"] == "event" and s["logs"][0]["kind"] == "draw" and "data" in s["logs"][0]:
            e = s["logs"][0]
            seat, n = e["data"]["seat"], e["data"]["n"]
            before = rep["steps"][k - 1]["state"]["players"][seat]["hand_count"]
            assert s["state"]["players"][seat]["hand_count"] == before + n


def test_replay_judge_and_draw_are_separate_steps():
    g = blank_game(["关羽", "孙权"])
    give(g, 1, "乐不思蜀")
    cid = g.players[1].hand.pop()
    g.players[1].judges.append((cid, "乐不思蜀"))
    rec = Recorder(g)
    choose(g, "结束出牌阶段")
    texts = [s["logs"][0]["text"] for s in rec.steps if s["type"] == "event"]
    i = next(k for k, t in enumerate(texts) if "判定牌为" in t)
    j = next(k for k, t in enumerate(texts) if "摸了 2 张牌（摸牌阶段）" in t)
    assert j > i  # 判定牌、判定结果、摸牌各是一步
    assert any("乐不思蜀" in t for t in texts[i + 1:j])


def test_clone_drops_log_hook():
    rec = Recorder.new_game("duel", seed=1, generals=["关羽", "吕布"])
    n = len(rec.steps)
    c = rec.g.clone()
    assert c.log_hook is None
    c.act(len(c.pending.options) - 1)
    assert len(rec.steps) == n


def test_live_session_hides_ai_hand():
    from sgs.tools.serve import Session
    s = Session(0, ["关羽", "吕布"], seed=2)
    rep = s.replay()
    for k, st in enumerate(rep["steps"]):
        if st["type"] == "decision" and k != len(rep["steps"]) - 1:
            assert st["decision"]["player"] == 0  # AI 被问了什么不发给浏览器
        if st["type"] == "event":
            assert st["logs"]
        assert "hand" not in st["state"]["players"][1]
        assert "deck_top" not in st["state"]
        for e in st["logs"]:
            assert "1" not in {str(k) for k in e.get("private", {})}


# ------------------------------------------------------------------ 规则修正
def test_first_turn_draws_two():
    g = Game(seed=1, generals=["关羽", "吕布"])
    assert any(e["text"] == "关羽[0] 摸了 2 张牌（摸牌阶段）" and e["turn"] == 1 for e in g.logs)


def test_biyue_is_optional():
    g = blank_game(["貂蝉", "关羽"])
    choose(g, "结束出牌阶段")
    assert "闭月" in g.pending.prompt
    choose(g, "否")
    assert not g.players[0].hand
    assert any("没有发动【闭月】" in t for t in g.visible_logs(None))


def test_skill_descriptions_match_flags():
    from sgs.core import registry as R
    for name, sk in R.SKILLS.items():
        if sk.equip:
            continue
        if sk.compulsory and sk.events:
            assert "锁定技" in sk.description, name
        if sk.events and not sk.compulsory:
            assert "可以" in sk.description, name
