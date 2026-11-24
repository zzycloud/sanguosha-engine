"""实体牌与虚拟牌。

实体牌（CardInfo）在一局里是不可变的，用整数 id 表示，状态里只存 id，
这样深拷贝/序列化都很便宜。虚拟牌（VCard）表示“当作某牌使用/打出”的那张牌，
可以由 0 张（八卦阵视为闪）、1 张（武圣）或多张（丈八蛇矛）实体牌转化而来。
"""
from __future__ import annotations

from typing import NamedTuple, Optional, Tuple

SPADE, HEART, CLUB, DIAMOND = "♠", "♥", "♣", "♦"
SUITS = (SPADE, HEART, CLUB, DIAMOND)
RED_SUITS = (HEART, DIAMOND)
BLACK_SUITS = (SPADE, CLUB)

RANK_STR = {1: "A", 11: "J", 12: "Q", 13: "K"}


def rank_str(rank: int) -> str:
    return RANK_STR.get(rank, str(rank))


def suit_color(suit: Optional[str]) -> Optional[str]:
    if suit in RED_SUITS:
        return "red"
    if suit in BLACK_SUITS:
        return "black"
    return None


class CardInfo(NamedTuple):
    id: int
    name: str
    suit: str
    rank: int

    @property
    def color(self) -> Optional[str]:
        return suit_color(self.suit)

    def __str__(self) -> str:  # 例：♠7杀
        return f"{self.suit}{rank_str(self.rank)}{self.name}"


class VCard:
    """一次使用/打出中“这张牌”的样子。"""

    __slots__ = ("name", "suit", "rank", "subcards", "skill")

    def __init__(self, name: str, subcards: Tuple[int, ...] = (), suit: Optional[str] = None,
                 rank: int = 0, skill: Optional[str] = None):
        self.name = name
        self.subcards = tuple(subcards)
        self.suit = suit
        self.rank = rank
        self.skill = skill

    @classmethod
    def real(cls, info: CardInfo) -> "VCard":
        return cls(info.name, (info.id,), info.suit, info.rank)

    @classmethod
    def convert(cls, g, name: str, subcards, skill: str) -> "VCard":
        """把若干实体牌转化为 name。单张保留花色点数，多张仅在同色时保留颜色。"""
        subcards = tuple(subcards)
        if len(subcards) == 1:
            info = g.cards[subcards[0]]
            return cls(name, subcards, info.suit, info.rank, skill)
        suit = None
        if subcards:
            colors = {g.cards[c].color for c in subcards}
            if len(colors) == 1:
                # 用一个代表花色记录颜色（多张同色牌转化时花色不确定，只保留颜色）
                suit = "red*" if colors == {"red"} else "black*"
        return cls(name, subcards, suit, 0, skill)

    @property
    def color(self) -> Optional[str]:
        if self.suit in ("red*",):
            return "red"
        if self.suit in ("black*",):
            return "black"
        return suit_color(self.suit)

    @property
    def is_real(self) -> bool:
        return self.skill is None and len(self.subcards) == 1

    def key(self):
        return (self.name, self.subcards, self.skill)

    def describe(self, g) -> str:
        if self.is_real:
            return str(g.cards[self.subcards[0]])
        subs = "+".join(str(g.cards[c]) for c in self.subcards)
        src = f"{self.skill}:" if self.skill else ""
        return f"{self.name}({src}{subs})" if subs else f"{self.name}({self.skill})"

    def __repr__(self) -> str:
        return f"VCard({self.name!r}, {self.subcards}, skill={self.skill!r})"
