"""1v1 两人对决模式。

规则（可配置）：
- 两名玩家各一名武将，体力和技能按武将牌；双方互为敌人，一方阵亡即另一方获胜。
- 起始手牌各 4 张；0 号位先行动，先手第一个摸牌阶段少摸 1 张（first_draw_penalty）。
- 选将：generals=[a, b] 直接指定；pick="random" 从将池随机；pick="choose" 双方依次从各自 3 名候选里选一名（选将也是决策，可用于训练）。
- 主公技不生效（1v1 没有主公）。

这是“两人对决”的简化规则，不是官方 1v1 赛制（官方赛制是多武将轮换上场）。
赛制不同可以另写一个 Mode 注册，不影响引擎。
"""
from typing import List, Optional, Sequence

from ..core.actions import TurnFrame
from ..core.base import Decision, Frame, Option
from ..core.plugin import Mode
from ..core.registry import GENERALS, register_mode


@register_mode
class DuelMode(Mode):
    name = "duel"
    num_players = 2

    def __init__(self, generals: Optional[Sequence[str]] = None, pick: str = "random",
                 pool: Optional[Sequence[str]] = None, start_hand: int = 4, first_draw_penalty: int = 1,
                 candidates: int = 3):
        self.generals = list(generals) if generals else None
        self.pick = pick
        self.pool = list(pool) if pool else None
        self.start_hand = start_hand
        self.first_draw_penalty = first_draw_penalty
        self.candidates = candidates

    def _pool(self) -> List[str]:
        return self.pool or [n for n, g in GENERALS.items() if g.pack == "standard"]

    def setup(self, g):
        for p in g.players:
            p.role = "duelist"
        if self.generals:
            for seat, name in enumerate(self.generals):
                assign_general(g, seat, name)
        elif self.pick == "random":
            names = g.rng.sample(self._pool(), 2)
            for seat, name in enumerate(names):
                assign_general(g, seat, name)

    def first_frame(self, g) -> Frame:
        return DuelStart(self)

    def draw_phase_count(self, g, seat, base):
        if g.turn_count == 1 and seat == 0:
            return max(0, base - self.first_draw_penalty)
        return base

    def on_death(self, g, seat, killer):
        alive = [p.seat for p in g.players if p.alive]
        if len(alive) == 1:
            g.finish(alive[0], f"{g.pname(alive[0])} 获胜")


def assign_general(g, seat: int, name: str) -> None:
    gen = GENERALS[name]
    p = g.players[seat]
    p.general = name
    p.max_hp = p.hp = gen.hp
    p.gender = gen.gender
    p.kingdom = gen.kingdom
    p.skills = list(gen.skills)


class DuelStart(Frame):
    """（可选）选将 → 发起始手牌 → 进入 0 号位的第一个回合。"""

    def __init__(self, mode: DuelMode):
        self.mode = mode
        self.seat = 0

    def step(self, g):
        m = self.mode
        if self.stage == 0:
            if g.players[0].general is None:
                pool = [n for n in m._pool()]
                g.rng.shuffle(pool)
                self.cands = [pool[i * m.candidates:(i + 1) * m.candidates] for i in range(2)]
                self.stage = 1
            else:
                self.stage = 3
        if self.stage == 1:
            if self.seat >= 2:
                self.stage = 3
            else:
                self.stage = 2
                opts = [Option(("general", n), f"{n}（{GENERALS[n].kingdom} {GENERALS[n].hp} 血："
                                              f"{'、'.join(GENERALS[n].skills)}）") for n in self.cands[self.seat]]
                g.ask(Decision(self.seat, "choose", "选择你的武将", opts, {"reason": "pick_general"}))
                return
        if self.stage == 2:
            name = g.answer().key[1]
            assign_general(g, self.seat, name)
            g.log(f"{self.seat} 号位选择了 {name}", "pick")
            self.seat += 1
            self.stage = 1
            return
        for p in g.players:
            g.log(f"{p.seat} 号位：{p.general}（{p.kingdom}，{p.hp} 体力，技能：{'、'.join(p.skills)}）", "setup")
        for p in g.players:
            g.draw(p.seat, m.start_hand, "起始手牌")
        g.done()
        g.push(TurnFrame(0))
