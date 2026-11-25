"""简单规则 AI：按固定优先级行动，用作基线和冷启动数据来源。

这里的 g 是 PlayerView（玩家视角快照），不是 Game，所以读不到对手手牌和牌堆。
"""
from __future__ import annotations

import random
from typing import Optional

from ..core.actions import JUDGE_GOOD
from ..core.registry import CARDS
from .base import Agent

VALUE = {"桃": 10, "无中生有": 8, "无懈可击": 7, "闪": 6, "顺手牵羊": 6, "杀": 5, "过河拆桥": 5, "决斗": 5,
         "乐不思蜀": 6, "南蛮入侵": 4, "万箭齐发": 4, "五谷丰登": 3, "桃园结义": 3, "借刀杀人": 3, "闪电": 1}
HARMFUL = {"过河拆桥", "顺手牵羊", "决斗", "南蛮入侵", "万箭齐发", "乐不思蜀", "闪电", "借刀杀人"}
HELPFUL = {"无中生有", "桃园结义", "五谷丰登"}
PLAY_ORDER = ["equip", "桃", "无中生有", "五谷丰登", "桃园结义", "顺手牵羊", "过河拆桥", "乐不思蜀", "借刀杀人",
              "skill", "决斗", "南蛮入侵", "万箭齐发", "杀"]


def card_value(g, cid) -> float:
    name = g.card(cid).name
    spec = CARDS[name]
    if spec.type == "equip":
        return 4
    return VALUE.get(name, 3)


class RuleAgent(Agent):
    name = "rule"

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)

    def choose(self, g, d):
        fn = getattr(self, "_" + d.kind, None)
        i = fn(g, d) if fn else None
        return 0 if i is None else i

    # ---------------------------------------------------------- 出牌阶段
    def _play(self, g, d):
        me = d.player
        p = g.players[me]
        best, best_rank = None, len(PLAY_ORDER) + 1
        for i, o in enumerate(d.options):
            k = o.key
            if k[0] == "use":
                name, subs, skill, targets = k[1], k[2], k[3], k[4]
                spec = CARDS[name]
                if spec.type == "equip":
                    if spec.subtype in p.equips:
                        continue
                    cat = "equip"
                else:
                    cat = name
                    if name in HARMFUL and not any(g.is_enemy(me, t) for t in targets):
                        continue
                    if name == "闪电":
                        continue
                    if name == "桃园结义":
                        mine = sum(1 for t in targets if not g.is_enemy(me, t) and g.players[t].wounded)
                        if mine <= sum(1 for t in targets if g.is_enemy(me, t) and g.players[t].wounded):
                            continue
                    if name == "决斗":
                        mine = sum(1 for c in p.hand if g.card(c).name == "杀")
                        if mine == 0:
                            continue
                if cat not in PLAY_ORDER:
                    continue
                r = PLAY_ORDER.index(cat)
                # 转化牌：尽量用价值低的牌转化
                if skill:
                    r += 0.5 + sum(card_value(g, c) for c in subs) / 100
                    if any(g.card(c).name in ("桃", "无中生有", "无懈可击") for c in subs):
                        continue
            elif k[0] == "skill":
                if not self._want_skill(g, me, k[1]):
                    continue
                r = PLAY_ORDER.index("skill")
            else:
                continue
            if r < best_rank:
                best, best_rank = i, r
        return best if best is not None else len(d.options) - 1

    def _want_skill(self, g, me, name):
        p = g.players[me]
        if name == "苦肉":
            return p.hp >= 3 or any(g.card(c).name == "桃" for c in p.hand)
        if name in ("青囊", "结姻"):
            return p.wounded
        if name == "仁德":
            return len(p.hand) > p.hp
        if name == "制衡":
            return any(card_value(g, c) <= 3 for c in p.hand)
        return name in ("反间", "离间")

    # ---------------------------------------------------------- 响应
    def _respond(self, g, d):
        me = d.player
        ctx = d.ctx
        names = ctx.get("names", [])
        pass_i = len(d.options) - 1
        if "无懈可击" in names:
            t, trick = ctx.get("target"), ctx.get("trick")
            want = (trick in HARMFUL and g.is_enemy(me, t)) or (trick in HELPFUL and not g.is_enemy(me, t))
            if trick in HELPFUL and g.is_enemy(me, t):
                want = False
            nullified = ctx.get("nullified", False)
            if (not nullified and not want and (trick in HARMFUL or trick in HELPFUL)) or (nullified and want):
                return 0
            return pass_i
        if "桃" in names and "dying" in ctx:
            return 0 if not g.is_enemy(me, ctx["dying"]) else pass_i
        if ctx.get("skill") == "鬼才":
            return self._guicai(g, d)
        if ctx.get("skill") == "青龙偃月刀" or ctx.get("collateral"):
            return 0
        # 闪/杀：优先八卦阵，其次实体牌
        for i, o in enumerate(d.options):
            if o.key[0] == "skill":
                return i
        return 0 if len(d.options) > 1 else pass_i

    def _guicai_card(self, g, d):
        """鬼才：有能把判定改成对自己有利的手牌就返回它，否则 None。"""
        ctx = d.ctx
        me, seat, reason = d.player, ctx["judge_seat"], ctx["judge_reason"]
        good = JUDGE_GOOD.get(reason)
        if good is None:
            return None
        want_good = not g.is_enemy(me, seat)
        if good(g.card(ctx["judge_card"])) == want_good:
            return None
        for c in g.hand:
            if good(g.card(c)) == want_good:
                return c
        return None

    def _guicai(self, g, d):
        c = self._guicai_card(g, d)
        for i, o in enumerate(d.options):
            if o.key == ("card", c):
                return i
        return 0

    # ---------------------------------------------------------- 是否发动
    def _trigger(self, g, d):
        if d.ctx.get("skill") == "鬼才":
            return 0 if self._guicai_card(g, d) is not None else 1
        return 0

    # ---------------------------------------------------------- 其它选择
    def _choose(self, g, d):
        me = d.player
        keys = [o.key for o in d.options]
        reason = d.ctx.get("reason", "") or d.ctx.get("skill", "")
        if all(k[0] in ("card", "stop") for k in keys):
            cards = [(i, k[1]) for i, k in enumerate(keys) if k[0] == "card"]
            stop = next((i for i, k in enumerate(keys) if k[0] == "stop"), None)
            if reason == "五谷丰登":
                return max(cards, key=lambda x: card_value(g, x[1]))[0]
            if "target" in d.ctx:  # 选对方区域里的牌：优先防具/武器
                return self._pick_enemy_card(g, d)
            low = min(cards, key=lambda x: card_value(g, x[1])) if cards else None
            if stop is not None and reason not in ("discard_phase",):
                if low is None or card_value(g, low[1]) > 3 or d.ctx.get("chosen"):
                    if reason in ("制衡",) or d.ctx.get("chosen"):
                        return stop
            return low[0] if low else stop
        if "target" in d.ctx:
            return self._pick_enemy_card(g, d)
        if reason == "pick_general":
            return self.rng.randrange(len(d.options))
        if reason == "刚烈":
            return 0 if g.players[me].hand_count >= 4 else 1
        if reason == "雌雄双股剑":
            return 1 if g.players[me].hand_count <= 2 else 0
        if d.ctx.get("skill") == "遗计":
            return next(i for i, k in enumerate(keys) if k[1] == me)
        if d.ctx.get("skill") == "观星":
            tops = [(i, k[1]) for i, k in enumerate(keys) if k[0] == "top"]
            return max(tops, key=lambda x: card_value(g, x[1]))[0]
        # 选目标：优先敌人
        for i, k in enumerate(keys):
            if k[0] == "target" and len(k) > 1 and isinstance(k[1], int):
                skill = d.ctx.get("skill")
                friendly = skill in ("青囊", "仁德")
                if g.is_enemy(me, k[1]) != friendly:
                    return i
        return 0

    def _pick_enemy_card(self, g, d):
        tgt = d.ctx.get("target")
        best, score = 0, -1
        for i, o in enumerate(d.options):
            k = o.key
            if k == ("hand",):
                s = 2
            else:
                cid = k[1]
                spec = CARDS[g.card(cid).name]
                zone = g.zone_of(cid)[0]
                if zone == "j":
                    s = 5 if not g.is_enemy(d.player, tgt) else 0
                elif spec.subtype == "armor":
                    s = 4
                elif spec.subtype == "weapon":
                    s = 3
                else:
                    s = 1
            if s > score:
                best, score = i, s
        return best
