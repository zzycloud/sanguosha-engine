"""对局状态和结算引擎。"""
from __future__ import annotations

import pickle
import random
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import registry as R
from .base import Decision, Frame, Option
from .cards import CardInfo, VCard, rank_str
from .plugin import INF

EQUIP_SLOTS = ("weapon", "armor", "plus_horse", "minus_horse")
SLOT_NAMES = {"weapon": "武器", "armor": "防具", "plus_horse": "+1马", "minus_horse": "-1马"}


class GameOver(Exception):
    pass


class Player:
    def __init__(self, seat: int):
        self.seat = seat
        self.general: Optional[str] = None
        self.hp = 0
        self.max_hp = 0
        self.gender = "male"
        self.kingdom = ""
        self.hand: List[int] = []
        self.equips: Dict[str, int] = {}
        self.judges: List[Tuple[int, str]] = []  # (实体牌 id, 当作的牌名)，后放的在后
        self.skills: List[str] = []
        self.alive = True
        self.marks: Dict[str, Any] = {}   # 跨回合的技能数据
        self.turn: Dict[str, Any] = {}    # 本回合数据，回合结束清空
        self.role = ""

    @property
    def wounded(self) -> bool:
        return self.hp < self.max_hp

    def card_count(self, zones: str = "he") -> int:
        n = 0
        if "h" in zones:
            n += len(self.hand)
        if "e" in zones:
            n += len(self.equips)
        if "j" in zones:
            n += len(self.judges)
        return n


class Game:
    """一局游戏。用法：

        g = Game("duel", seed=1, generals=["关羽", "吕布"])
        while not g.over:
            d = g.pending            # 当前要谁做什么决定
            g.act(index)             # 选 d.options[index]
    """

    def __init__(self, mode: str = "duel", seed: int = 0, deck: str = "standard", log: bool = True,
                 auto_single: bool = True, max_turns: int = 200, **mode_config):
        R.load_all()
        self.seed = seed
        self.rng = random.Random(seed)
        self.mode_name = mode
        self.mode = R.MODES[mode](**mode_config)
        self.deck_name = deck
        cards = R.DECKS[deck]()
        self.cards: Tuple[CardInfo, ...] = tuple(CardInfo(i, n, s, r) for i, (n, s, r) in enumerate(cards))
        self.deck: List[int] = list(range(len(self.cards)))  # 末尾是牌堆顶
        self.rng.shuffle(self.deck)
        self.discard: List[int] = []
        self.processing: List[int] = []
        self.players: List[Player] = [Player(i) for i in range(self.mode.num_players)]
        self.stack: List[Frame] = []
        self.pending: Optional[Decision] = None
        self._answer: Optional[Option] = None
        self.ret: Any = None
        self.current = 0          # 当前回合角色
        self.phase = ""
        self.turn_count = 0
        self.max_turns = max_turns
        self.over = False
        self.winner: Optional[int] = None  # 获胜座位；平局为 None
        self.result = ""
        self.logging = log
        self.logs: List[Dict[str, Any]] = []
        self.auto_single = auto_single
        self.history: List[int] = []
        # 每个已完成的决策（含自动跳过的单选项决策），只在 log=True 时记录
        self.decisions: List[Dict[str, Any]] = []
        self.steps = 0
        self.queued: List[int] = []  # 失去牌后待触发 lose_card 事件的座位
        self.mode.setup(self)
        self.push(self.mode.first_frame(self))
        self.run()

    # ------------------------------------------------------------------ 引擎
    def push(self, frame: Frame) -> None:
        self.stack.append(frame)

    def done(self, result: Any = None) -> None:
        self.stack.pop()
        self.ret = result

    def ask(self, d: Decision) -> None:
        if not d.options:
            raise RuntimeError(f"没有选项的决策: {d}")
        # auto_single=True：只有一个选项的决策自动选择；"responses"：出牌阶段除外（便于写测试）
        if self.auto_single and len(d.options) == 1 and (self.auto_single is True or d.kind != "play"):
            if self.logging:
                self._record_decision(d, 0, True)
            self._answer = d.options[0]
            return
        self.pending = d

    def answer(self) -> Option:
        a = self._answer
        self._answer = None
        if a is None:
            raise RuntimeError("没有可取的回答")
        return a

    def run(self) -> None:
        stack = self.stack
        try:
            while stack and self.pending is None and not self.over:
                self.steps += 1
                stack[-1].step(self)
                if self.queued and self.pending is None and self._answer is None:
                    from .actions import QueuedTrigger
                    for seat in reversed(self.queued):
                        self.push(QueuedTrigger("lose_card", seat, self.ret))
                    self.queued = []
        except GameOver:
            pass
        if not stack and not self.over:
            self.finish(None, "结算栈清空")

    def act(self, index: int) -> None:
        """选择当前决策的第 index 个选项并推进到下一个决策。"""
        d = self.pending
        if d is None:
            raise RuntimeError("当前没有待做的决策")
        if not 0 <= index < len(d.options):
            raise IndexError(f"选项越界: {index} / {len(d.options)}")
        self.history.append(index)
        if self.logging:
            self._record_decision(d, index, False)
        self._answer = d.options[index]
        self.pending = None
        self.run()

    def _record_decision(self, d: Decision, index: int, auto: bool) -> None:
        self.decisions.append({
            "i": len(self.decisions), "log_i": len(self.logs), "turn": self.turn_count, "phase": self.phase,
            "player": d.player, "kind": d.kind, "prompt": d.prompt,
            "options": [{"key": _plain(o.key), "label": o.label} for o in d.options],
            "ctx": _plain(d.ctx), "chosen": index, "auto": auto,
        })

    def act_key(self, key: Tuple) -> None:
        for i, o in enumerate(self.pending.options):
            if o.key == key:
                return self.act(i)
        raise KeyError(key)

    def legal_actions(self) -> List[Option]:
        return [] if self.pending is None else self.pending.options

    def finish(self, winner: Optional[int], reason: str) -> None:
        self.over = True
        self.winner = winner
        self.result = reason
        self.pending = None
        self.log(f"游戏结束：{reason}")
        raise GameOver()

    def clone(self) -> "Game":
        """完整拷贝（含随机数状态）。用于 rollout。"""
        return pickle.loads(pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL))

    # ------------------------------------------------------------------ 日志
    def log(self, text: str, kind: str = "info", private: Optional[Dict[int, str]] = None, **data) -> None:
        if not self.logging:
            return
        entry = {"i": len(self.logs), "turn": self.turn_count, "kind": kind, "text": text}
        if private:
            entry["private"] = private
        if data:
            entry["data"] = data
        self.logs.append(entry)

    def pname(self, seat: int) -> str:
        p = self.players[seat]
        return f"{p.general}[{seat}]"

    def cstr(self, cid: int) -> str:
        return str(self.cards[cid])

    # ------------------------------------------------------------------ 查询
    def spec(self, name: str):
        return R.CARDS[name]

    def skill(self, name: str):
        return R.SKILLS[name]

    def alive_seats(self, start: Optional[int] = None) -> List[int]:
        """从 start（默认当前回合角色）开始按座次排列的存活角色。"""
        n = len(self.players)
        s = self.current if start is None else start
        return [(s + i) % n for i in range(n) if self.players[(s + i) % n].alive]

    def others(self, seat: int) -> List[int]:
        return [s for s in self.alive_seats(seat) if s != seat]

    def next_alive(self, seat: int) -> int:
        n = len(self.players)
        for i in range(1, n + 1):
            s = (seat + i) % n
            if self.players[s].alive:
                return s
        return seat

    def skills_of(self, seat: int) -> List[str]:
        p = self.players[seat]
        out = list(p.skills)
        for slot in EQUIP_SLOTS:
            cid = p.equips.get(slot)
            if cid is not None:
                sk = R.CARDS[self.cards[cid].name].skill
                if sk:
                    out.append(sk)
        return out

    def has_skill(self, seat: int, name: str) -> bool:
        return name in self.skills_of(seat)

    def all_skills(self) -> Iterable[Tuple[int, Any]]:
        for s in self.alive_seats():
            for name in self.skills_of(s):
                yield s, R.SKILLS[name]

    def has_listener(self, event: str) -> bool:
        for _, sk in self.all_skills():
            if event in sk.events:
                return True
        return False

    def distance(self, a: int, b: int) -> int:
        if a == b:
            return 0
        alive = self.alive_seats(a)
        idx = alive.index(b)
        base = min(idx, len(alive) - idx)
        pa, pb = self.players[a], self.players[b]
        if "plus_horse" in pb.equips:
            base += 1
        if "minus_horse" in pa.equips:
            base -= 1
        for s, sk in self.all_skills():
            base += sk.distance_mod(self, s, a, b)
        return max(1, base)

    def attack_range(self, seat: int) -> int:
        p = self.players[seat]
        cid = p.equips.get("weapon")
        r = R.CARDS[self.cards[cid].name].weapon_range if cid is not None else 1
        for s, sk in self.all_skills():
            if s == seat:
                r += sk.range_mod(self, s)
        return r

    def in_attack_range(self, a: int, b: int) -> bool:
        return a != b and self.distance(a, b) <= self.attack_range(a)

    def slash_limit(self, seat: int) -> int:
        n = 1
        for s, sk in self.all_skills():
            if s == seat:
                n += sk.slash_limit_mod(self, s)
        return n

    def max_hand(self, seat: int) -> int:
        n = max(0, self.players[seat].hp)
        for s, sk in self.all_skills():
            if s == seat:
                n += sk.max_hand_mod(self, s)
        return n

    def prohibited(self, user: int, target: int, vc: VCard) -> bool:
        for s, sk in self.all_skills():
            if sk.prohibit(self, s, user, target, vc):
                return True
        return False

    def ignores_distance(self, user: int, vc: VCard) -> bool:
        for s, sk in self.all_skills():
            if s == user and sk.ignore_distance(self, s, user, vc):
                return True
        return False

    def is_enemy(self, a: int, b: int) -> bool:
        return self.mode.is_enemy(self, a, b)

    # ------------------------------------------------------------------ 牌的移动
    def _reshuffle(self) -> None:
        if not self.discard:
            self.finish(None, "牌堆和弃牌堆都已用尽，平局")
        self.deck = self.discard
        self.discard = []
        self.rng.shuffle(self.deck)
        self.log("弃牌堆洗入牌堆", "shuffle")

    def draw_top(self) -> int:
        if not self.deck:
            self._reshuffle()
        return self.deck.pop()

    def peek_top(self, n: int) -> List[int]:
        """从牌堆顶取 n 张（不移动），顶在前。"""
        while len(self.deck) < n:
            if not self.discard:
                break
            rest = self.discard
            self.discard = []
            self.rng.shuffle(rest)
            self.deck = rest + self.deck
        return list(reversed(self.deck[-n:])) if n else []

    def draw(self, seat: int, n: int, reason: str = "") -> List[int]:
        got = []
        for _ in range(n):
            got.append(self.draw_top())
        self.players[seat].hand.extend(got)
        if self.logging and got:
            self.log(f"{self.pname(seat)} 摸了 {n} 张牌" + (f"（{reason}）" if reason else ""), "draw",
                     private={seat: "、".join(self.cstr(c) for c in got)}, seat=seat, n=n)
        return got

    def locate(self, cid: int) -> Tuple[str, Optional[int]]:
        for p in self.players:
            if cid in p.hand:
                return "h", p.seat
            for slot, c in p.equips.items():
                if c == cid:
                    return "e", p.seat
            for c, _ in p.judges:
                if c == cid:
                    return "j", p.seat
        if cid in self.processing:
            return "processing", None
        if cid in self.discard:
            return "discard", None
        if cid in self.deck:
            return "deck", None
        return "?", None

    def remove_card(self, cid: int) -> Tuple[str, Optional[int]]:
        """把一张牌从它当前所在区域拿走，返回 (区域, 拥有者)。"""
        for p in self.players:
            if cid in p.hand:
                p.hand.remove(cid)
                self._after_lose(p.seat, "h")
                return "h", p.seat
            for slot, c in list(p.equips.items()):
                if c == cid:
                    del p.equips[slot]
                    self._after_lose(p.seat, "e")
                    return "e", p.seat
            for j in p.judges:
                if j[0] == cid:
                    p.judges.remove(j)
                    return "j", p.seat
        if cid in self.processing:
            self.processing.remove(cid)
            return "processing", None
        if cid in self.discard:
            self.discard.remove(cid)
            return "discard", None
        if cid in self.deck:
            self.deck.remove(cid)
            return "deck", None
        raise ValueError(f"找不到牌 {cid}")

    def _after_lose(self, seat: int, zone: str) -> None:
        # 失去牌的时机事件（连营、枭姬）。排到当前这一步结束后统一触发。
        p = self.players[seat]
        if not p.alive:
            return
        for name in self.skills_of(seat):
            if "lose_card" in R.SKILLS[name].events:
                p.marks.setdefault("_lostq", []).append(zone)
                if seat not in self.queued:
                    self.queued.append(seat)
                return

    def to_hand(self, seat: int, cids: Sequence[int]) -> None:
        for c in cids:
            self.remove_card(c)
            self.players[seat].hand.append(c)

    def to_discard(self, cids: Sequence[int]) -> None:
        for c in cids:
            self.remove_card(c)
            self.discard.append(c)

    def to_processing(self, cids: Sequence[int]) -> None:
        for c in cids:
            self.remove_card(c)
            self.processing.append(c)

    def clear_processing(self, cids: Optional[Sequence[int]] = None) -> None:
        for c in list(self.processing if cids is None else cids):
            if c in self.processing:
                self.processing.remove(c)
                self.discard.append(c)

    def put_deck_top(self, cids: Sequence[int]) -> None:
        """cids[0] 放到最上面。"""
        for c in reversed(list(cids)):
            self.remove_card(c)
            self.deck.append(c)

    def put_deck_bottom(self, cids: Sequence[int]) -> None:
        for c in cids:
            self.remove_card(c)
            self.deck.insert(0, c)

    # ------------------------------------------------------------------ 体力
    def recover(self, seat: int, n: int = 1, source: Optional[int] = None) -> int:
        p = self.players[seat]
        real = min(n, p.max_hp - p.hp)
        if real > 0:
            p.hp += real
            self.log(f"{self.pname(seat)} 回复 {real} 点体力（{p.hp}/{p.max_hp}）", "recover", seat=seat, n=real)
        return real

    # ------------------------------------------------------------------ 观察
    def card_dict(self, cid: int) -> Dict[str, Any]:
        c = self.cards[cid]
        return {"id": c.id, "name": c.name, "suit": c.suit, "rank": rank_str(c.rank)}

    def player_view(self, seat: int, viewer: Optional[int]) -> Dict[str, Any]:
        """viewer=None 表示上帝视角。"""
        p = self.players[seat]
        d = {
            "seat": seat, "general": p.general, "kingdom": p.kingdom, "gender": p.gender,
            "hp": p.hp, "max_hp": p.max_hp, "alive": p.alive, "skills": list(p.skills),
            "hand_count": len(p.hand),
            "equips": {slot: self.card_dict(c) for slot, c in p.equips.items()},
            "judges": [dict(self.card_dict(c), as_name=n) for c, n in p.judges],
            "marks": {k: v for k, v in p.marks.items() if not k.startswith("_")},
        }
        if viewer is None or viewer == seat:
            d["hand"] = [self.card_dict(c) for c in p.hand]
        return d

    def observe(self, viewer: Optional[int]) -> Dict[str, Any]:
        """viewer 视角下的观察（JSON 友好）。viewer=None 为上帝视角。

        玩家视角来自 PlayerView：看不到对手手牌、牌堆顺序；当前决策只有决策者本人能看到，
        其他人也不知道此刻在等谁（避免从“被询问无懈可击”推断出手牌）。
        """
        if viewer is not None:
            obs = self.view(viewer, derived=True).to_dict()
            obs["over"], obs["winner"] = self.over, self.winner
            d = self.pending
            if d is not None and d.player == viewer:
                obs["decision"] = {"player": d.player, "kind": d.kind, "prompt": d.prompt,
                                   "options": [o.label for o in d.options], "ctx": _plain(d.ctx)}
            return obs
        obs = {
            "viewer": viewer,
            "turn": self.turn_count, "current": self.current, "phase": self.phase,
            "players": [self.player_view(s, viewer) for s in range(len(self.players))],
            "deck_count": len(self.deck),
            "discard": [self.card_dict(c) for c in self.discard[-20:]],
            "discard_count": len(self.discard),
            "processing": [self.card_dict(c) for c in self.processing],
            "over": self.over, "winner": self.winner,
        }
        obs["deck_top"] = [self.card_dict(c) for c in reversed(self.deck[-5:])]
        d = self.pending
        if d is not None:
            obs["decision"] = {"player": d.player, "kind": d.kind, "prompt": d.prompt,
                               "options": [o.label for o in d.options], "ctx": _plain(d.ctx)}
        return obs

    def view(self, viewer: int, with_logs: bool = False, derived: bool = False):
        """viewer 的视角快照（不含 Game 引用）。智能体只应通过它看对局。
        derived=True 时额外计算距离、攻击范围、杀的次数上限、手牌上限。"""
        from .view import PlayerView
        return PlayerView(self, viewer, with_logs, derived)

    def visible_logs(self, viewer: Optional[int], since: int = 0) -> List[str]:
        out = []
        for e in self.logs[since:]:
            t = log_text(e, viewer)
            if t is not None:
                out.append(t)
        return out

    def determinize(self, viewer: int, rng: Optional[random.Random] = None) -> "Game":
        """返回一个拷贝：把 viewer 看不到的牌（其他人手牌 + 牌堆）随机重新分配。
        用于隐藏信息下的 rollout。注意：观星等技能带来的牌堆顶信息不会保留。"""
        rng = rng or random.Random()
        g = self.clone()
        unknown = list(g.deck)
        counts = []
        for p in g.players:
            if p.seat != viewer:
                counts.append((p.seat, len(p.hand)))
                unknown.extend(p.hand)
        rng.shuffle(unknown)
        for seat, n in counts:
            g.players[seat].hand = unknown[:n]
            unknown = unknown[n:]
        g.deck = unknown
        g.rng = random.Random(rng.random())
        return g


def log_text(e: Dict[str, Any], viewer: Optional[int]) -> Optional[str]:
    """一条日志在 viewer 视角下的文本；看不到的返回 None。viewer=None 为上帝视角。"""
    text = e["text"]
    priv = e.get("private")
    extra = None
    if priv:
        if viewer is None:
            vals = []
            for v in priv.values():
                if v not in vals:
                    vals.append(v)
            extra = "；".join(vals)
        elif viewer in priv:
            extra = priv[viewer]
    if not text:
        return extra
    return f"{text}：{extra}" if extra else text


def _plain(x):
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, VCard):
        return {"name": x.name, "subcards": list(x.subcards), "skill": x.skill}
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)
