"""写单测/复现局面用的小工具：指定手牌、牌堆顶、装备，然后按选项文字操作。"""
from __future__ import annotations

from typing import Iterable, Optional

from .core.game import Game


def blank_game(generals, seed: int = 0, **kw) -> Game:
    """双方都没有起始手牌、先手第一回合不摸牌，停在 0 号位出牌阶段。"""
    auto = kw.pop("auto_single", "responses")
    g = Game("duel", seed=seed, generals=list(generals), start_hand=0, first_draw_penalty=2,
             auto_single=False, **kw)
    g.auto_single = auto
    return g


def find(g: Game, name: str, suit: Optional[str] = None, rank: Optional[int] = None,
         zones: Iterable[str] = ("deck", "discard")) -> int:
    for c in g.cards:
        if c.name == name and (suit is None or c.suit == suit) and (rank is None or c.rank == rank):
            if g.locate(c.id)[0] in zones:
                return c.id
    raise LookupError(f"牌堆里找不到 {suit or ''}{rank or ''}{name}")


def _parse(spec: str):
    """'杀' / '♥杀' / '♠7杀' → (name, suit, rank)"""
    suit = rank = None
    if spec and spec[0] in "♠♥♣♦":
        suit, spec = spec[0], spec[1:]
        num = ""
        while spec and (spec[0].isdigit() or spec[0] in "AJQK"):
            num += spec[0]
            spec = spec[1:]
        if num:
            rank = {"A": 1, "J": 11, "Q": 12, "K": 13}.get(num) or int(num)
    return spec, suit, rank


def refresh(g: Game) -> None:
    """改动局面后，重新生成出牌阶段的选项。"""
    from .core.actions import play_options
    d = g.pending
    if d is not None and d.kind == "play":
        d.options = play_options(g, d.player)


def give(g: Game, seat: int, *specs: str):
    out = []
    for s in specs:
        cid = find(g, *_parse(s))
        g.to_hand(seat, [cid])
        g.known.pop(cid, None)     # 测试里塞进手牌的牌不算谁知道
        out.append(cid)
    g.queued = []
    refresh(g)
    return out


def equip(g: Game, seat: int, *specs: str):
    for s in specs:
        cid = find(g, *_parse(s))
        g.remove_card(cid)
        slot = g.spec(g.cards[cid].name).subtype
        g.players[seat].equips[slot] = cid
    g.queued = []
    refresh(g)


def stack_deck(g: Game, *specs: str):
    """把指定牌按顺序放到牌堆顶（specs[0] 最先被摸到/判定）。"""
    cids = [find(g, *_parse(s), zones=("deck", "discard")) for s in specs]
    g.put_deck_top(cids)
    return cids


def choose(g: Game, text: str) -> None:
    """选择 label 包含 text 的第一个选项。"""
    d = g.pending
    assert d is not None, "没有待做的决策"
    for i, o in enumerate(d.options):
        if text in o.label:
            g.act(i)
            return
    raise AssertionError(f"在 {d.prompt} 里找不到选项 {text!r}，现有：{[o.label for o in d.options]}")


def labels(g: Game):
    return [o.label for o in g.pending.options] if g.pending else []


def pass_until(g: Game, kind: str = "play", seat: Optional[int] = None, limit: int = 50) -> None:
    """一路选“不出/否/不使用/不发动”，直到出现 kind 类型的决策。"""
    for _ in range(limit):
        d = g.pending
        if d is None:
            return
        if d.kind == kind and (seat is None or d.player == seat):
            return
        for i, o in enumerate(d.options):
            if o.key in (("pass",), ("no",), ("stop",), ("end",)):
                g.act(i)
                break
        else:
            g.act(0)
    raise AssertionError("pass_until 超出步数")
