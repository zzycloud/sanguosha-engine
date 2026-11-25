"""锦囊对某名角色不会产生效果时，跳过这名角色：不问无懈可击，也不结算。"""
import random

from sgs import Game
from sgs.agents.base import RandomAgent, decide
from sgs.testing import blank_game, choose, give, labels


def logs(g):
    return g.visible_logs(None)


def test_god_salvation_skips_full_hp_without_nullify_question():
    g = blank_game(["关羽", "张飞"])
    g.players[1].hp = 3
    give(g, 0, "桃园结义", "♣Q无懈可击")
    give(g, 1, "♠J无懈可击")
    choose(g, "桃园结义")
    # 关羽满血：直接跳过，第一个无懈询问就是针对张飞的
    d = g.pending
    assert d.kind == "respond" and d.ctx["trick"] == "桃园结义" and d.ctx["target"] == 1
    assert any("【桃园结义】对 关羽[0] 没有效果（体力已满）" in t for t in logs(g))
    choose(g, "不使用")
    choose(g, "不使用")
    assert g.players[1].hp == 4 and g.players[0].hp == 4
    assert g.pending.kind == "play" and g.pending.player == 0


def test_god_salvation_only_asks_for_wounded_user():
    g = blank_game(["关羽", "张飞"])
    g.players[0].hp = 2
    give(g, 0, "桃园结义", "♣Q无懈可击")
    give(g, 1, "♠J无懈可击")
    choose(g, "桃园结义")
    asked = []
    while g.pending.kind == "respond":
        asked.append(g.pending.ctx["target"])
        choose(g, "不使用")
    assert set(asked) == {0} and g.players[0].hp == 3
    assert any("对 张飞[1] 没有效果（体力已满）" in t for t in logs(g))


def test_god_salvation_not_offered_when_everyone_full():
    g = blank_game(["张飞", "关羽"])   # 张飞没有武圣，不会把它当杀
    give(g, 0, "桃园结义")
    assert not any("桃园结义" in x for x in labels(g))


def test_dismantle_no_effect_after_target_spends_last_card_on_nullify():
    g = blank_game(["关羽", "张飞"])
    give(g, 0, "过河拆桥", "♣Q无懈可击")
    give(g, 1, "♠J无懈可击")
    choose(g, "过河拆桥")
    choose(g, "不使用")          # 关羽先放过
    choose(g, "使用 ")           # 张飞用最后一张牌抵消
    choose(g, "使用 ")           # 关羽再抵消：过河拆桥仍然生效，但张飞已经没有牌
    assert g.pending.kind == "play" and g.pending.player == 0
    assert any("【过河拆桥】对 张飞[1] 没有效果（区域里没有牌）" in t for t in logs(g))


def test_random_games_never_ask_nullify_for_no_effect_targets():
    """随机对局里，无懈可击的询问只出现在效果真的会发生的目标上。"""
    checked = 0
    for seed in range(300):
        g = Game("duel", seed=seed, pick="random", auto_single=False)
        ags = [RandomAgent(seed), RandomAgent(seed + 1)]
        n = 0
        while not g.over and n < 3000:
            d = g.pending
            t = d.ctx.get("target") if d.ctx else None
            if d.kind == "respond" and d.ctx.get("names") == ["无懈可击"] and t is not None:
                p = g.players[t]
                if d.ctx["trick"] == "桃园结义":
                    assert p.wounded, (seed, n)
                    checked += 1
                if d.ctx["trick"] in ("过河拆桥", "顺手牵羊"):
                    assert p.card_count("hej"), (seed, n)
                if d.ctx["trick"] == "借刀杀人":
                    assert "weapon" in p.equips, (seed, n)
            g.act(decide(ags[d.player], g))
            n += 1
    assert checked > 0
