"""锦囊对某名角色不会产生效果时，跳过这名角色：不问无懈可击，也不结算。"""
import random

from sgs import Game
from sgs.agents.base import RandomAgent, decide
from sgs.testing import blank_game, choose, equip, give, labels


def logs(g):
    return g.visible_logs(None)


def test_god_salvation_targets_only_wounded():
    """桃园结义只以受伤的角色为目标，满血角色不是目标，也就不会对它询问无懈可击。"""
    g = blank_game(["关羽", "张飞"])
    g.players[1].hp = 3
    give(g, 0, "桃园结义", "♣Q无懈可击")
    give(g, 1, "♠J无懈可击")
    choose(g, "桃园结义")
    asked = []
    while g.pending.kind == "respond":
        asked.append(g.pending.ctx["target"])
        choose(g, "不使用")
    assert set(asked) == {1}
    assert g.players[1].hp == 4 and g.players[0].hp == 4
    use = [t for t in logs(g) if "使用了" in t and "桃园结义" in t]
    assert use and "张飞" in use[-1] and "关羽[0]" not in use[-1].split("使用了")[1]


def test_god_salvation_not_offered_when_everyone_full():
    g = blank_game(["张飞", "关羽"])   # 张飞没有武圣，不会把它当杀
    give(g, 0, "桃园结义")
    assert not any("桃园结义" in x for x in labels(g))


# ------------------------------------------------------------------ 出牌时的合法目标
def offered(g, name):
    """出牌选项里 name 这张牌（不含转化）能指定的目标描述。"""
    return [x for x in labels(g) if x.startswith("使用 ") and name in x and "(" not in x]


def test_dismantle_and_snatch_need_target_with_cards():
    g = blank_game(["张飞", "孙权"])
    give(g, 0, "过河拆桥", "顺手牵羊")
    assert not offered(g, "过河拆桥") and not offered(g, "顺手牵羊")   # 孙权没有任何牌
    equip(g, 1, "八卦阵")
    assert offered(g, "过河拆桥") and offered(g, "顺手牵羊")


def test_snatch_distance_uses_horses_not_weapon_range():
    g = blank_game(["张飞", "孙权"])
    give(g, 0, "顺手牵羊")
    give(g, 1, "闪")
    assert offered(g, "顺手牵羊")                   # 1v1 距离为 1
    equip(g, 1, "绝影")                             # 对方 +1 马：距离 2
    assert not offered(g, "顺手牵羊")
    equip(g, 0, "麒麟弓")                           # 武器攻击范围 5，但顺手看距离不看攻击范围
    assert not offered(g, "顺手牵羊")
    equip(g, 0, "赤兔")                             # 自己 -1 马：距离回到 1
    assert offered(g, "顺手牵羊")


def test_snatch_qicai_ignores_distance_and_qianxun_blocks():
    g = blank_game(["黄月英", "孙权"])
    give(g, 0, "顺手牵羊")
    give(g, 1, "闪")
    equip(g, 1, "绝影")
    assert offered(g, "顺手牵羊")                   # 奇才：锦囊无距离限制
    g = blank_game(["张飞", "陆逊"])
    give(g, 0, "顺手牵羊")
    give(g, 1, "闪")
    assert not offered(g, "顺手牵羊")               # 谦逊


def test_other_tricks_have_no_distance_limit():
    g = blank_game(["张飞", "孙权"])
    give(g, 0, "过河拆桥", "决斗", "乐不思蜀")
    give(g, 1, "闪")
    equip(g, 1, "绝影")
    for name in ("过河拆桥", "决斗", "乐不思蜀"):
        assert offered(g, name), name


def test_collateral_needs_weapon_and_victim_in_its_range():
    g = blank_game(["张飞", "孙权"])
    give(g, 0, "借刀杀人")
    assert not offered(g, "借刀杀人")               # 孙权没有武器
    equip(g, 1, "诸葛连弩")
    assert offered(g, "借刀杀人")                   # 张飞在孙权的攻击范围 1 内
    equip(g, 0, "绝影")                             # 张飞 +1 马：孙权够不到
    assert not offered(g, "借刀杀人")
    equip(g, 1, "青龙偃月刀")                       # 换成攻击范围 3 的武器
    assert offered(g, "借刀杀人")


def test_slash_with_equipped_weapon_uses_range_after_removal():
    """关羽用装备着的红色武器当杀：攻击范围按武器离开后计算。"""
    g = blank_game(["关羽", "孙权"])
    equip(g, 0, "♥5麒麟弓")
    equip(g, 1, "绝影")
    give(g, 0, "♦2闪")
    ls = labels(g)
    assert any("武圣:♦2闪" in x for x in ls)        # 手牌当杀：有麒麟弓，范围够
    assert not any("武圣:♥5麒麟弓" in x for x in ls)  # 武器自己当杀：范围变回 1，够不到 +1 马


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
            g.act(decide(ags[d.player], g))
            n += 1
    assert checked > 0
