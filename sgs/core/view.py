"""玩家视角：智能体和导出工具只能通过它看对局。

PlayerView 是在决策点生成的一份快照，内部不持有 Game 的引用，所以拿到它的代码
没有办法读到对手手牌、牌堆顺序或别人的私密日志。看不见的牌用 view.card() 去查会报错。

能看到的牌：自己的手牌、所有人的装备区和判定区、处理区、弃牌堆，以及当前决策选项里
出现的牌（例如观星看到的牌堆顶、五谷丰登亮出的牌、鬼才要改的判定牌）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .cards import CardInfo, rank_str


class HiddenCardError(KeyError):
    """查询了当前视角看不到的牌。"""


class PublicPlayer:
    """一名角色的公开信息（加上 viewer 自己的手牌）。"""

    __slots__ = ("seat", "general", "kingdom", "gender", "hp", "max_hp", "alive", "skills",
                 "hand_count", "equips", "judges", "hand", "marks")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def wounded(self) -> bool:
        return self.hp < self.max_hp

    def card_count(self, zones: str = "he") -> int:
        n = 0
        if "h" in zones:
            n += self.hand_count
        if "e" in zones:
            n += len(self.equips)
        if "j" in zones:
            n += len(self.judges)
        return n


def decision_card_ids(d) -> List[int]:
    """当前决策里明确给出、因而决策者能看到的牌。"""
    out: List[int] = []
    if d is None:
        return out
    for o in d.options:
        k = o.key
        if not k:
            continue
        if k[0] in ("card", "top", "bottom") and len(k) > 1 and isinstance(k[1], int):
            out.append(k[1])
        elif k[0] in ("use", "play") and len(k) > 2:
            out.extend(c for c in k[2] if isinstance(c, int))
    jc = d.ctx.get("judge_card") if d.ctx else None
    if isinstance(jc, int):
        out.append(jc)
    return out


class PlayerView:
    """viewer 在当前时刻能看到的全部信息。"""

    def __init__(self, g, viewer: int, with_logs: bool = False, derived: bool = False):
        self.viewer = viewer
        self.seat = viewer
        self.turn = g.turn_count
        self.current = g.current
        self.phase = g.phase
        self.mode = g.mode_name
        self.deck_count = len(g.deck)
        self.discard: Tuple[int, ...] = tuple(g.discard)
        self.processing: Tuple[int, ...] = tuple(g.processing)
        n = len(g.players)
        self.num_players = n
        self.players: List[PublicPlayer] = []
        visible = set(g.discard) | set(g.processing)
        zone: Dict[int, Tuple[str, int]] = {}
        for p in g.players:
            for slot, c in p.equips.items():
                visible.add(c)
                zone[c] = ("e", p.seat)
            for c, _ in p.judges:
                visible.add(c)
                zone[c] = ("j", p.seat)
            own = p.seat == viewer
            if own:
                visible.update(p.hand)
                for c in p.hand:
                    zone[c] = ("h", p.seat)
            self.players.append(PublicPlayer(
                seat=p.seat, general=p.general, kingdom=p.kingdom, gender=p.gender, hp=p.hp,
                max_hp=p.max_hp, alive=p.alive, skills=tuple(p.skills), hand_count=len(p.hand),
                equips=dict(p.equips), judges=tuple(p.judges), hand=tuple(p.hand) if own else None,
                marks={k: v for k, v in p.marks.items() if not k.startswith("_")}))
        for c in g.discard:
            zone[c] = ("d", None)
        for c in g.processing:
            zone[c] = ("p", None)
        d = g.pending if (g.pending is not None and g.pending.player == viewer) else None
        visible.update(decision_card_ids(d))
        self._cards: Dict[int, CardInfo] = {c: g.cards[c] for c in visible}
        self._zone = zone
        self.slash_used = g.players[viewer].turn.get("slash", 0)
        # 距离、攻击范围等派生量计算较慢，derived=True 时才算（导出数据时打开）
        self.distance: Optional[Dict[Tuple[int, int], int]] = None
        self.attack_range: Optional[Dict[int, int]] = None
        self.slash_limit: Optional[int] = None
        self.max_hand: Optional[int] = None
        if derived:
            alive = [s for s in range(n) if g.players[s].alive]
            self.distance = {(a, b): g.distance(a, b) for a in alive for b in alive}
            self.attack_range = {s: g.attack_range(s) for s in alive}
            self.slash_limit = g.slash_limit(viewer) if g.players[viewer].alive else 0
            self.max_hand = g.max_hand(viewer)
        self.enemy = {s: g.mode.known_enemy(g, viewer, s) for s in range(n)}
        self.logs = g.visible_logs(viewer) if with_logs else None

    # ------------------------------------------------------------- 查询
    @property
    def me(self) -> PublicPlayer:
        return self.players[self.viewer]

    @property
    def hand(self) -> Tuple[int, ...]:
        return self.me.hand

    def card(self, cid: int) -> CardInfo:
        try:
            return self._cards[cid]
        except KeyError:
            raise HiddenCardError(f"第 {cid} 号牌在 {self.viewer} 号位的视角下不可见") from None

    def can_see(self, cid: int) -> bool:
        return cid in self._cards

    def zone_of(self, cid: int) -> Tuple[str, Optional[int]]:
        """可见牌所在区域：h 手牌 / e 装备 / j 判定 / d 弃牌堆 / p 处理区；其它可见牌返回 ("?", None)。"""
        self.card(cid)
        return self._zone.get(cid, ("?", None))

    def is_enemy(self, a: int, b: Optional[int] = None) -> bool:
        """is_enemy(t)：t 是否是我的敌人。为了兼容也可写 is_enemy(me, t)。"""
        t = a if b is None else b
        return bool(self.enemy.get(t, False))

    def in_attack_range(self, a: int, b: int) -> bool:
        if self.distance is None:
            raise ValueError("这个视角没有计算距离；用 g.view(seat, derived=True)")
        return a != b and (a, b) in self.distance and self.distance[(a, b)] <= self.attack_range[a]

    def to_dict(self) -> Dict[str, Any]:
        """可 JSON 序列化的视角观察。"""
        def cd(c):
            ci = self._cards[c]
            return {"id": ci.id, "name": ci.name, "suit": ci.suit, "rank": rank_str(ci.rank)}

        players = []
        for p in self.players:
            d = {"seat": p.seat, "general": p.general, "kingdom": p.kingdom, "gender": p.gender,
                 "hp": p.hp, "max_hp": p.max_hp, "alive": p.alive, "skills": list(p.skills),
                 "hand_count": p.hand_count,
                 "equips": {slot: cd(c) for slot, c in p.equips.items()},
                 "judges": [dict(cd(c), as_name=n) for c, n in p.judges],
                 "marks": _plain(p.marks)}
            if p.hand is not None:
                d["hand"] = [cd(c) for c in p.hand]
            players.append(d)
        return {
            "viewer": self.viewer, "turn": self.turn, "current": self.current, "phase": self.phase,
            "players": players, "deck_count": self.deck_count,
            "discard_count": len(self.discard), "discard_top": [cd(c) for c in self.discard[-10:]],
            "processing": [cd(c) for c in self.processing],
            "slash_used": self.slash_used, "slash_limit": self.slash_limit, "max_hand": self.max_hand,
            "distance": None if self.distance is None else
            {f"{a}->{b}": v for (a, b), v in self.distance.items() if a != b},
            "attack_range": None if self.attack_range is None else
            {str(k): v for k, v in self.attack_range.items()},
        }


def _plain(x):
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_plain(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)
