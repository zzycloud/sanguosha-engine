"""关键结算的单元测试。规则参考标准版规则书和无名杀的实现。"""
import random

import pytest

from sgs import Game
from sgs.agents.base import RandomAgent, decide, play_game
from sgs.testing import blank_game, choose, equip, give, labels, pass_until, stack_deck


def hp(g, s):
    return g.players[s].hp


def end_turn(g):
    choose(g, "结束出牌阶段")


# ------------------------------------------------------------------ 无懈可击
def test_nullify_chain_odd_cancels():
    g = blank_game(["关羽", "张飞"])
    give(g, 0, "无中生有")
    give(g, 1, "无懈可击")
    choose(g, "使用 ♥")  # 无中生有都是红桃
    assert g.pending.player == 1 and "无懈可击" in g.pending.prompt
    choose(g, "使用 ")
    assert len(g.players[0].hand) == 0  # 被抵消，没摸牌


def test_nullify_chain_even_takes_effect():
    g = blank_game(["关羽", "张飞"])
    give(g, 0, "无中生有", "♣Q无懈可击")
    give(g, 1, "♠J无懈可击")
    choose(g, "无中生有")
    # 张飞无懈 → 关羽再无懈 → 张飞没牌了
    assert g.pending.player == 0  # 先问使用者（当前回合角色）
    choose(g, "不使用")
    choose(g, "使用 ")       # 张飞抵消
    assert g.pending.player == 0 and "已被抵消" in g.pending.prompt
    choose(g, "使用 ")       # 关羽抵消抵消
    assert len(g.players[0].hand) == 2  # 生效，摸两张


# ------------------------------------------------------------------ 杀的次数与诸葛连弩
def test_slash_limit_and_crossbow():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "♠7杀")
    assert not any("杀" in l for l in labels(g)), labels(g)
    g2 = blank_game(["关羽", "孙权"])
    give(g2, 0, "♠7杀", "♠8杀")
    equip(g2, 0, "♣诸葛连弩")
    choose(g2, "♠7杀")
    assert any("♠8杀" in l for l in labels(g2))


def test_paoxiao_unlimited():
    g = blank_game(["张飞", "孙权"])
    give(g, 0, "♠7杀", "♠8杀", "♠9杀")
    for c in ("♠7杀", "♠8杀", "♠9杀"):
        choose(g, c)
    assert hp(g, 1) == 1


# ------------------------------------------------------------------ 判定：闪电、乐不思蜀
def test_lightning_hits_on_spade_2_9():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠A闪电")
    choose(g, "闪电")
    end_turn(g)
    stack_deck(g, "♠5绝影")  # 关羽下回合的判定牌（孙权摸牌后的下一张）
    # 孙权回合：不出牌，结束
    end_turn(g)
    # 第 3 回合关羽判定阶段：没人有无懈，自动判定
    assert hp(g, 0) == 1, g.visible_logs(None)[-5:]
    assert not g.players[0].judges


def test_lightning_moves_on_other_cards():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠A闪电")
    choose(g, "闪电")
    end_turn(g)
    stack_deck(g, "♥A桃园结义")
    end_turn(g)  # 关羽判定闪电
    assert hp(g, 0) == 4
    assert g.players[1].judges and g.players[1].judges[0][1] == "闪电"


def test_indulgence_skips_play_unless_heart():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠6乐不思蜀")
    choose(g, "乐不思蜀")
    stack_deck(g, "♠7杀")  # 判定牌：黑桃 → 跳过出牌
    end_turn(g)
    # 孙权直接跳过出牌阶段，回到关羽的回合
    assert g.current == 0 and g.pending.player == 0
    assert any("跳过出牌阶段" in t for t in g.visible_logs(None))


def test_indulgence_heart_no_skip():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠6乐不思蜀")
    choose(g, "乐不思蜀")
    stack_deck(g, "♥3桃")
    end_turn(g)
    assert g.current == 1 and g.pending.kind == "play"


def test_indulgence_can_be_nullified_at_judge_time():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠6乐不思蜀")
    give(g, 1, "无懈可击")
    choose(g, "乐不思蜀")
    end_turn(g)
    assert "无懈可击" in g.pending.prompt and g.pending.player == 1
    choose(g, "使用 ")
    assert g.current == 1 and g.pending.kind == "play"


# ------------------------------------------------------------------ 濒死求桃
def test_dying_saved_by_peach():
    g = blank_game(["关羽", "孙权"])
    g.players[1].hp = 1
    give(g, 0, "♠7杀")
    give(g, 1, "♥3桃")
    choose(g, "杀")
    # 濒死：先问当前回合角色关羽（没有桃，自动跳过），再问孙权自己
    assert g.pending.player == 1 and "濒死" in g.pending.prompt
    choose(g, "使用 ")
    assert hp(g, 1) == 1 and g.players[1].alive and not g.over


def test_dying_without_peach_dies():
    g = blank_game(["关羽", "孙权"])
    g.players[1].hp = 1
    give(g, 0, "♠7杀")
    choose(g, "杀")
    assert g.over and g.winner == 0


def test_jijiu_peach_outside_own_turn():
    g = blank_game(["关羽", "华佗"])
    g.players[1].hp = 1
    give(g, 0, "♠7杀")
    give(g, 1, "♦5贯石斧")
    choose(g, "杀")
    assert "急救" in " ".join(labels(g))
    choose(g, "急救")
    assert hp(g, 1) == 1


# ------------------------------------------------------------------ 技能触发与改判
def test_wushuang_needs_two_dodges():
    g = blank_game(["吕布", "孙权"])
    give(g, 0, "♠7杀")
    give(g, 1, "♦2闪")
    choose(g, "杀")
    choose(g, "使用 ")   # 只有一张闪
    assert hp(g, 1) == 3


def test_wushuang_duel_needs_two_slashes():
    g = blank_game(["吕布", "孙权"])
    give(g, 0, "♠A决斗")
    give(g, 1, "♠7杀")
    choose(g, "决斗")
    choose(g, "打出 ")   # 打出一张后还需要一张，没了 → 受伤
    assert hp(g, 1) == 3


def test_tieqi_red_no_dodge():
    g = blank_game(["马超", "孙权"])
    give(g, 0, "♠7杀")
    give(g, 1, "♦2闪")
    stack_deck(g, "♥3桃")
    choose(g, "杀")
    choose(g, "是")       # 发动铁骑
    assert hp(g, 1) == 3  # 不能出闪，直接命中


def test_guicai_retrial_then_tiandu_gets_final_card():
    # 司马懿改判 郭嘉的判定，天妒拿到的是改判后的牌
    g = blank_game(["司马懿", "郭嘉"])
    give(g, 0, "♠6乐不思蜀", "♥3桃")
    choose(g, "乐不思蜀")
    stack_deck(g, "♥4桃")   # 原判定红桃（不跳过）
    end_turn(g)
    # 判定牌 ♥4桃 → 司马懿鬼才改成黑色？他手里只有 ♥3桃，改成另一张红桃
    assert "鬼才" in g.pending.prompt
    choose(g, "是")         # 非锁定技：先问是否发动；手里只有 ♥3桃，选牌自动
    assert "天妒" in g.pending.prompt
    choose(g, "是")
    hand = [g.cards[c] for c in g.players[1].hand]
    assert any(c.suit == "♥" and c.rank == 3 for c in hand)


def test_guicai_changes_indulgence_result():
    g = blank_game(["司马懿", "孙权"])
    give(g, 0, "♠6乐不思蜀", "♠7杀")
    choose(g, "乐不思蜀")
    stack_deck(g, "♥4桃")  # 红桃：本来不跳过
    end_turn(g)
    choose(g, "是")         # 发动鬼才，手里只有 ♠7杀，自动改判为黑桃
    assert g.current == 0   # 孙权跳过出牌阶段


def test_ganglie_after_damage():
    g = blank_game(["关羽", "夏侯惇"])
    give(g, 0, "♠7杀")
    stack_deck(g, "♠8杀")
    choose(g, "杀")
    choose(g, "是")  # 刚烈
    # 关羽没有手牌，只能受到伤害
    assert hp(g, 0) == 3 and hp(g, 1) == 3


def test_fankui_takes_card():
    g = blank_game(["关羽", "司马懿"])
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "♠7杀")
    choose(g, "是")
    assert len(g.players[1].hand) == 1


def test_jianxiong_obtains_slash():
    g = blank_game(["关羽", "曹操"])
    give(g, 0, "♠7杀")
    choose(g, "杀")
    choose(g, "是")
    assert [g.cards[c].name for c in g.players[1].hand] == ["杀"]


def test_kongcheng_blocks_slash():
    g = blank_game(["关羽", "诸葛亮"])
    give(g, 0, "♠7杀", "♠A决斗")
    assert not any("杀" in l or "决斗" in l for l in labels(g) if "使用" in l)


def test_qianxun_blocks_snatch():
    g = blank_game(["关羽", "陆逊"])
    give(g, 0, "♠3顺手牵羊")
    give(g, 1, "♠8杀")
    assert not any("顺手牵羊" in l for l in labels(g))


# ------------------------------------------------------------------ 装备
def test_bagua_and_qinggang():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀")
    equip(g, 1, "♠2八卦阵")
    stack_deck(g, "♥3桃")
    choose(g, "杀")
    assert any("八卦阵" in l for l in labels(g))
    choose(g, "八卦阵")
    assert hp(g, 1) == 4
    g2 = blank_game(["关羽", "孙权"])
    give(g2, 0, "♠7杀")
    equip(g2, 0, "青釭剑")
    equip(g2, 1, "♠2八卦阵")
    choose(g2, "杀")
    assert not any("八卦阵" in l for l in labels(g2))


def test_renwang_black_slash_no_effect():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀", "♥10杀")
    equip(g, 1, "仁王盾")
    choose(g, "♠7杀")
    assert hp(g, 1) == 4 and g.pending.player == 0
    g2 = blank_game(["张飞", "孙权"])
    give(g2, 0, "♥10杀")
    equip(g2, 1, "仁王盾")
    choose(g2, "♥10杀")
    assert hp(g2, 1) == 3


def test_horses_distance():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀", "♠3顺手牵羊")
    equip(g, 1, "绝影")
    assert g.distance(0, 1) == 2
    assert not any("杀" in l or "顺手" in l for l in labels(g) if l.startswith("使用"))
    equip(g, 0, "赤兔")
    assert g.distance(0, 1) == 1


def test_blade_followup_slash():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀", "♠8杀")
    give(g, 1, "♦2闪")
    equip(g, 0, "青龙偃月刀")
    choose(g, "♠7杀")
    choose(g, "使用 ")
    assert "青龙偃月刀" in g.pending.prompt
    choose(g, "是")         # 只有一张 ♠8杀，自动使用
    assert hp(g, 1) == 3


def test_ice_sword_discards_instead():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♠7杀")
    give(g, 1, "♠8杀", "♠9杀")
    equip(g, 0, "寒冰剑")
    choose(g, "♠7杀")
    choose(g, "是")  # 发动寒冰剑；对方只有手牌，“随机一张手牌”是唯一选项，自动选择两次
    assert hp(g, 1) == 4 and not g.players[1].hand


def test_collateral_give_weapon():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "♣Q借刀杀人")
    equip(g, 1, "青釭剑")
    choose(g, "借刀杀人")
    # 孙权没有杀，只能交出武器（单一选项自动选择）
    assert any(g.cards[c].name == "青釭剑" for c in g.players[0].hand)


def test_amazing_grace():
    g = blank_game(["关羽", "孙权"])
    give(g, 0, "五谷丰登")
    stack_deck(g, "♠7杀", "♥3桃")
    choose(g, "五谷丰登")
    choose(g, "桃")
    assert [g.cards[c].name for c in g.players[0].hand] == ["桃"]
    assert [g.cards[c].name for c in g.players[1].hand] == ["杀"]


# ------------------------------------------------------------------ rollout 需求
def test_clone_is_deterministic():
    g = Game(seed=7, generals=["诸葛亮", "司马懿"], auto_single=False)
    ag = [RandomAgent(1), RandomAgent(2)]
    for _ in range(30):
        g.act(decide(ag[g.pending.player], g))
    c = g.clone()
    seq = []
    rng = random.Random(3)
    while not g.over:
        i = rng.randrange(len(g.pending.options))
        seq.append(i)
        g.act(i)
    for i in seq:
        c.act(i)
    assert c.over and c.winner == g.winner and c.logs == g.logs


def test_same_seed_same_game():
    def run():
        g = Game(seed=11, log=True)
        return play_game(g, [RandomAgent(5), RandomAgent(6)])
    a, b = run(), run()
    assert a.logs == b.logs and a.history == b.history


def test_observation_hides_opponent_hand():
    g = Game(seed=3, generals=["关羽", "孙权"])
    obs = g.observe(0)
    assert "hand" in obs["players"][0] and "hand" not in obs["players"][1]
    assert obs["players"][1]["hand_count"] == 4
    assert "deck_top" not in obs


def test_determinize_keeps_own_hand():
    g = Game(seed=3, generals=["关羽", "孙权"])
    d = g.determinize(0, random.Random(1))
    assert d.players[0].hand == g.players[0].hand
    assert len(d.players[1].hand) == len(g.players[1].hand)
    assert sorted(d.deck + d.players[1].hand) == sorted(g.deck + g.players[1].hand)


@pytest.mark.parametrize("seed", range(60))
def test_random_games_conserve_cards(seed):
    from sgs import GENERALS, load_all
    load_all()
    names = list(GENERALS)
    rng = random.Random(seed)
    a, b = rng.sample(names, 2)
    g = Game(seed=seed, generals=[a, b], log=False, auto_single=bool(seed % 2))
    play_game(g, [RandomAgent(seed), RandomAgent(seed + 1)])
    n = len(g.deck) + len(g.discard) + len(g.processing)
    n += sum(len(p.hand) + len(p.equips) + len(p.judges) for p in g.players)
    assert g.over and n == len(g.cards)


# ------------------------------------------------------------------ 更多武将技能
def test_kurou_self_dying_and_peach():
    g = blank_game(["黄盖", "孙权"])
    g.players[0].hp = 1
    give(g, 0, "♥3桃")
    choose(g, "苦肉")
    assert "濒死" in g.pending.prompt and g.pending.player == 0
    choose(g, "使用 ")
    assert hp(g, 0) == 1 and len(g.players[0].hand) == 2


def test_lianying_draws_when_hand_empty():
    g = blank_game(["陆逊", "孙权"])
    give(g, 0, "♠7杀")
    choose(g, "杀")
    choose(g, "是")  # 连营
    assert len(g.players[0].hand) == 1


def test_xiaoji_draws_two_on_equip_loss():
    g = blank_game(["关羽", "孙尚香"])
    give(g, 0, "♠3过河拆桥")
    equip(g, 1, "绝影")
    choose(g, "过河拆桥")
    choose(g, "是")
    assert len(g.players[1].hand) == 2


def test_keji_skips_discard():
    g = blank_game(["吕蒙", "孙权"])
    g.players[0].hp = 1
    give(g, 0, "♦2闪", "♦3闪", "♦4闪")
    choose(g, "结束出牌阶段")
    choose(g, "是")
    assert len(g.players[0].hand) == 3


def test_guanxing_reorders_deck():
    g = blank_game(["孙权", "诸葛亮"])
    choose(g, "结束出牌阶段")
    # 孙权回合摸两张后，下一回合是孙权？不：诸葛亮是 1 号位，第 2 回合开始就观星
    assert "观星" in g.pending.prompt or "观星" in g.pending.ctx.get("skill", "")


def test_zhiheng():
    g = blank_game(["孙权", "关羽"])
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "制衡")
    choose(g, "♠7杀")
    choose(g, "♠8杀")
    assert len(g.players[0].hand) == 2
    assert not any("制衡" in l for l in labels(g))


def test_tuxi_takes_card_instead_of_draw():
    g = blank_game(["关羽", "张辽"])
    give(g, 0, "♠7杀")
    choose(g, "结束出牌阶段")
    choose(g, "是")
    assert len(g.players[1].hand) == 1 and len(g.players[0].hand) == 0


def test_luoyi_extra_damage():
    g = blank_game(["关羽", "许褚"])
    give(g, 1, "♠7杀")
    choose(g, "结束出牌阶段")
    choose(g, "是")  # 裸衣
    choose(g, "♠7杀")
    assert hp(g, 0) == 2


def test_yiji_distributes():
    g = blank_game(["关羽", "郭嘉"])
    give(g, 0, "♠7杀")
    choose(g, "杀")
    choose(g, "是")
    choose(g, "自己留下")
    choose(g, "交给")
    assert len(g.players[1].hand) == 1 and len(g.players[0].hand) == 1


def test_luoshen_keeps_black():
    g = blank_game(["关羽", "甄姬"])
    stack_deck(g, "♠7杀", "♠8杀", "♥3桃")
    choose(g, "结束出牌阶段")
    choose(g, "是")
    choose(g, "是")
    choose(g, "是")
    # 两张黑色获得，红色停止；之后摸牌阶段再摸两张
    names = sorted(g.cards[c].name for c in g.players[1].hand)
    assert names.count("杀") >= 2


def test_qixi_black_as_dismantle():
    g = blank_game(["甘宁", "孙权"])
    give(g, 0, "♠7杀")
    give(g, 1, "♥3桃")
    assert any("过河拆桥(奇袭" in l for l in labels(g))


def test_rende_heals_after_two():
    g = blank_game(["刘备", "孙权"])
    g.players[0].hp = 3
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "仁德")
    choose(g, "♠7杀")
    choose(g, "♠8杀")
    assert hp(g, 0) == 4 and len(g.players[1].hand) == 2


def test_jieyin():
    g = blank_game(["孙尚香", "关羽"])
    g.players[0].hp = 2
    g.players[1].hp = 2
    give(g, 0, "♠7杀", "♠8杀")
    choose(g, "结姻")
    choose(g, "♠7杀")  # 第二张是唯一选项，自动选择
    assert hp(g, 0) == 3 and hp(g, 1) == 3


def test_fanjian_wrong_suit_damages():
    g = blank_game(["周瑜", "关羽"])
    choose(g, "否")  # 第一回合摸牌阶段不发动英姿
    give(g, 0, "♠7杀")
    choose(g, "反间")
    choose(g, "♥")
    assert hp(g, 1) == 3 and len(g.players[1].hand) == 1


def test_longdan_both_ways():
    g = blank_game(["关羽", "赵云"])
    give(g, 0, "♠7杀")
    give(g, 1, "♠8杀")
    choose(g, "杀")
    assert any("闪(龙胆" in l for l in labels(g))
