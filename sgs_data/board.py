"""摆局面：从一局空白对局出发，指定双方武将、体力、手牌、装备、判定区，再按脚本推进到要出题的那个决策点。

做法（全部用引擎的公开接口和 sgs.testing 里的小工具，不改引擎）：
1. blank_game 开一局双方都没手牌的对局，推进到“锚点”：某个座位的出牌阶段；
2. 在锚点上改局面：清空双方的牌放回牌堆，按要求发牌、装备、改体力、改回合数，随机弃掉一部分牌堆，让牌堆张数像真实对局；
3. 清掉开局的日志和已知手牌（摆出来的牌谁都不知道），刷新出牌阶段的选项；
4. 按脚本推进（比如“对手用杀打郭嘉”），其余决定交给规则 AI，直到停在要出题的决策点。

同一个 (场景, 种子) 永远摆出同一个局面，所以题目只存“配方”就能复现。
"""
from __future__ import annotations

import random
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from sgs.agents.base import decide
from sgs.agents.rule import RuleAgent
from sgs.core.cards import BLACK_SUITS
from sgs.core.game import Game
from sgs.core.registry import CARDS, GENERALS, load_all
from sgs.testing import blank_game, refresh

Script = Callable[[Game], Optional[int]]


class BuildFailed(Exception):
    """这个种子摆不出想要的局面（比如马超铁骑判定成功，郭嘉根本没机会出闪）。换个种子再摆。"""


def standard_generals() -> List[str]:
    load_all()
    return [n for n, g in GENERALS.items() if g.pack == "standard"]


class Board:
    def __init__(self, generals: Sequence[str], seed: int):
        self.seed = seed
        self.rng = random.Random(seed * 7919 + 17)
        self.g = blank_game(list(generals), seed=seed)
        self.filler = [RuleAgent(seed), RuleAgent(seed + 1)]

    # ------------------------------------------------------------------ 推进
    def until(self, pred: Callable[[Game], bool], scripts: Iterable[Script] = (), limit: int = 400) -> Game:
        """一直推进到 pred(g) 为真。每一步先问脚本（返回选项下标或 None），没有脚本管的决定交给规则 AI。"""
        g = self.g
        scripts = list(scripts)
        for _ in range(limit):
            if g.over or g.pending is None:
                raise BuildFailed("对局在出题前结束了")
            if pred(g):
                return g
            i = None
            for s in scripts:
                i = s(g)
                if i is not None:
                    break
            if i is None:
                i = decide(self.filler[g.pending.player], g)
            g.act(i)
        raise BuildFailed("推进步数超限")

    def to_play_phase(self, seat: int) -> Game:
        """推进到 seat 的出牌阶段（锚点）。别人的出牌阶段直接结束、别人要弃牌就随便弃。"""
        def end_others(g):
            d = g.pending
            if d.kind == "play" and d.player != seat:
                return len(d.options) - 1
            return None
        return self.until(lambda g: g.pending.kind == "play" and g.pending.player == seat, [end_others])

    # ------------------------------------------------------------------ 改局面
    def clear(self, seat: int) -> None:
        """把 seat 的手牌、装备、判定区的牌都放回牌堆。"""
        g = self.g
        p = g.players[seat]
        cids = list(p.hand) + list(p.equips.values()) + [c for c, _ in p.judges]
        for c in cids:
            g.remove_card(c)
            g.deck.append(c)
        g.queued = []

    def find(self, name: str, color: Optional[str] = None, suit: Optional[str] = None,
             exclude: Iterable[int] = ()) -> int:
        """牌堆或弃牌堆里随机找一张符合要求的牌。color 是 black / red。"""
        g = self.g
        ex = set(exclude)
        cands = []
        for zone_cards in (g.deck, g.discard):
            for c in zone_cards:
                info = g.cards[c]
                if c in ex or info.name != name:
                    continue
                if color and info.color != color:
                    continue
                if suit and info.suit != suit:
                    continue
                cands.append(c)
        if not cands:
            raise BuildFailed(f"找不到 {color or ''}{suit or ''}{name}")
        return self.rng.choice(cands)

    def _take(self, cid: int) -> None:
        g = self.g
        g.remove_card(cid)
        g.queued = []

    def give(self, seat: int, name: str, color: Optional[str] = None, suit: Optional[str] = None) -> int:
        cid = self.find(name, color, suit)
        self._take(cid)
        self.g.players[seat].hand.append(cid)
        return cid

    def give_random(self, seat: int, n: int, exclude_names: Iterable[str] = (),
                    only_types: Optional[Iterable[str]] = None, black_ok: bool = True) -> List[int]:
        """从牌堆随机发 n 张，牌名不在 exclude_names 里（可以限定牌的类型 basic/trick/equip，或不要黑色牌）。"""
        g = self.g
        ex = set(exclude_names)
        types = set(only_types) if only_types else None
        cands = [c for c in g.deck if g.cards[c].name not in ex
                 and (types is None or CARDS[g.cards[c].name].type in types)
                 and (black_ok or g.cards[c].suit not in BLACK_SUITS)]
        if len(cands) < n:
            raise BuildFailed("牌堆里可发的牌不够")
        out = self.rng.sample(cands, n)
        for c in out:
            self._take(c)
            g.players[seat].hand.append(c)
        return out

    def equip(self, seat: int, name: str) -> int:
        cid = self.find(name)
        self._take(cid)
        slot = CARDS[name].subtype
        p = self.g.players[seat]
        if slot in p.equips:
            old = p.equips.pop(slot)
            self.g.deck.append(old)
        p.equips[slot] = cid
        return cid

    def equip_random(self, seat: int, slots: Sequence[str], exclude_names: Iterable[str] = ()) -> List[int]:
        out = []
        ex = set(exclude_names)
        for slot in slots:
            names = sorted({self.g.cards[c].name for c in self.g.deck
                            if CARDS[self.g.cards[c].name].type == "equip" and CARDS[self.g.cards[c].name].subtype == slot
                            and self.g.cards[c].name not in ex})
            if names:
                out.append(self.equip(seat, self.rng.choice(names)))
        return out

    def judge(self, seat: int, name: str) -> int:
        cid = self.find(name)
        self._take(cid)
        self.g.players[seat].judges.append((cid, name))
        return cid

    def hp(self, seat: int, hp: int) -> None:
        p = self.g.players[seat]
        p.hp = max(1, min(hp, p.max_hp))

    def finish(self, turn: Optional[int] = None, discard: Optional[int] = None) -> Game:
        """改完局面：洗牌堆、随机弃掉一部分（牌堆张数像真实对局）、改回合数、清掉日志和已知手牌、刷新选项。"""
        g = self.g
        g.discard.extend([])  # 保留原来的弃牌堆
        self.rng.shuffle(g.deck)
        n = self.rng.randint(0, 40) if discard is None else discard
        n = min(n, max(0, len(g.deck) - 30))
        for _ in range(n):
            g.discard.append(g.deck.pop(0))
        g.turn_count = self.rng.randint(3, 14) if turn is None else turn
        g.known.clear()
        g.queued = []
        g.logs = []
        g.decisions = []
        g.auto_single = True
        for p in g.players:
            p.turn = {}
        refresh(g)
        return g


# ---------------------------------------------------------------------- 常用的脚本和判断
def use_card(seat: int, name: str, target: Optional[int] = None) -> Script:
    """seat 的出牌阶段：使用一张 name（有 target 时要以它为目标）。"""
    def s(g):
        d = g.pending
        if d.kind != "play" or d.player != seat:
            return None
        for i, o in enumerate(d.options):
            k = o.key
            if k[0] == "use" and k[1] == name and not k[3] and (target is None or target in k[4]):
                return i
        return None
    return s


def end_play(seat: int) -> Script:
    def s(g):
        d = g.pending
        if d.kind == "play" and d.player == seat:
            return len(d.options) - 1
        return None
    return s


def answer_trigger(seat: int, skill: str, yes: bool = True) -> Script:
    def s(g):
        d = g.pending
        if d.player == seat and d.kind == "trigger" and (d.ctx or {}).get("skill") == skill:
            want = ("yes",) if yes else ("no",)
            for i, o in enumerate(d.options):
                if o.key == want:
                    return i
        return None
    return s


def dont_respond(seat: int) -> Script:
    def s(g):
        d = g.pending
        if d.player == seat and d.kind == "respond":
            return len(d.options) - 1
        return None
    return s


def card_names(g: Game, cids: Iterable[int]) -> List[str]:
    return [str(g.cards[c]) for c in cids]


def summary(g: Game, seat: int) -> Dict[str, object]:
    """给人看的局面摘要（上帝视角，只用于 review.md，不进训练数据）。"""
    def side(s):
        p = g.players[s]
        return {"general": p.general, "hp": f"{p.hp}/{p.max_hp}", "hand": card_names(g, p.hand),
                "equips": card_names(g, p.equips.values()), "judges": [n for _, n in p.judges]}
    return {"turn": g.turn_count, "me": side(seat), "opp": side(1 - seat), "deck": len(g.deck)}
