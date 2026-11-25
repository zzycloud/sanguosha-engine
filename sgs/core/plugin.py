"""扩展点的基类：CardSpec（卡牌规则）、Skill（技能，含装备效果）、Mode（模式）。

所有方法都有“不起作用”的默认实现，子类只覆盖自己关心的钩子。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from .base import Frame
from .cards import VCard

INF = 10 ** 6


class CardSpec:
    name: str = ""
    type: str = "basic"         # basic / trick / delayed / equip
    subtype: Optional[str] = None  # weapon / armor / plus_horse / minus_horse
    skill: Optional[str] = None    # 装备牌附带的技能名
    weapon_range: int = 1
    response_only: bool = False    # 闪、无懈可击：不能在出牌阶段主动使用

    # ---- 出牌阶段 ----
    def play_targets(self, g, user: int, vc: VCard) -> List[Tuple[int, ...]]:
        """出牌阶段可用时返回所有合法目标组合；不能用返回 []。"""
        return []

    # ---- 结算 ----
    def effect(self, g, use, target: int) -> Optional[Frame]:
        """对一个目标生效时的结算（已经过无懈可击）。返回要压栈的 Frame 或 None。"""
        return None

    def split_targets(self, targets):
        """把出牌选项里的目标元组拆成 (真正的目标, 附加字段)。借刀杀人用：(A, B) -> ([A], {"victim": B})。"""
        return list(targets), {}

    def before_effects(self, g, use) -> Optional[Frame]:
        """所有目标结算前（五谷丰登亮牌）。"""
        return None

    def after_effects(self, g, use) -> None:
        """所有目标结算后（五谷丰登剩余牌进弃牌堆由引擎统一处理）。"""

    def delayed_effect(self, g, owner: int, cid: int, as_name: str) -> Optional[Frame]:
        """延时锦囊在判定阶段的结算。"""
        return None

    @property
    def is_trick(self) -> bool:
        return self.type in ("trick", "delayed")


class Skill:
    """技能基类。一个技能实例是无状态的单例，玩家相关的数据存在 player.marks 里。

    是否询问由引擎统一决定，技能自己不管：
    - 锁定技（locked = True，原文写“锁定技”）：时机到了强制生效，不询问；
    - 非锁定技：events 里的每个发动时机，引擎都先问“是否发动”，答“是”才执行；
    - effects：不是发动时机，而是已经发动的技能的后续效果（例如裸衣发动后本回合伤害 +1），到时直接执行，不询问。

    触发技：events 非空，实现 can_trigger 和 on_trigger / make_frame。
    转化技：实现 view_as。
    主动技（出牌阶段发动）：实现 can_activate 和 activate。
    规则修正：实现 distance_mod / range_mod / slash_limit_mod / max_hand_mod / prohibit 等。
    """

    name: str = ""
    events: Tuple[str, ...] = ()    # 发动时机（非锁定技会被询问）
    effects: Tuple[str, ...] = ()   # 后续效果的时机（从不询问）
    locked: bool = False            # 锁定技
    lord: bool = False
    equip: bool = False  # 装备技能（失去装备即失去）
    description: str = ""

    # ---- 触发技 ----
    def listens(self, event: str) -> bool:
        return event in self.events or event in self.effects

    def asks(self, event: str) -> bool:
        """引擎的统一规则：非锁定技在发动时机询问；锁定技和后续效果不询问。"""
        return not self.locked and event in self.events and event not in self.effects

    def trigger_ctx(self, g, owner: int, event: str, ev) -> dict:
        """询问“是否发动”时附带的上下文（给 AI 和提示词用），例如鬼才要改的判定牌。"""
        return {}

    def can_trigger(self, g, owner: int, event: str, ev) -> bool:
        return False

    def make_frame(self, g, owner: int, event: str, ev) -> Optional[Frame]:
        """默认用 CallFrame 调用 on_trigger；需要多步决策的技能覆盖本方法。"""
        from .actions import CallFrame
        return CallFrame(self.name, owner, event, ev)

    def on_trigger(self, g, owner: int, event: str, ev) -> None:
        pass

    def trigger_prompt(self, g, owner: int, event: str, ev) -> str:
        return f"是否发动【{self.name}】？"

    # ---- 转化技 ----
    def view_as(self, g, owner: int, names: Optional[Iterable[str]], ctx) -> List[VCard]:
        """返回能“当作”某牌的虚拟牌。names=None 表示出牌阶段主动使用（任意牌名）；
        否则是被要求的牌名集合。ctx 里 'mode' 为 'play' / 'use' / 'respond'。"""
        return []

    # ---- 响应时的特殊选项（如八卦阵：判定代替闪）----
    def response_options(self, g, owner: int, names, ctx) -> List[str]:
        return []

    def response_frame(self, g, owner: int, names, ctx) -> Optional[Frame]:
        return None

    # ---- 主动技 ----
    def can_activate(self, g, owner: int) -> bool:
        return False

    def activate(self, g, owner: int) -> Optional[Frame]:
        return None

    # ---- 规则修正 ----
    def distance_mod(self, g, owner: int, frm: int, to: int) -> int:
        return 0

    def range_mod(self, g, owner: int) -> int:
        return 0

    def slash_limit_mod(self, g, owner: int) -> int:
        return 0

    def max_hand_mod(self, g, owner: int) -> int:
        return 0

    def prohibit(self, g, owner: int, user: int, target: int, vc: VCard) -> bool:
        """owner 的技能是否禁止 user 对 target 使用 vc。"""
        return False

    def ignore_distance(self, g, owner: int, user: int, vc: VCard) -> bool:
        return False

    def armor_active(self, g, owner: int, ctx) -> bool:
        """防具技能用：被无视防具时失效。"""
        return not (ctx and ctx.get("ignore_armor"))


class Mode:
    name: str = ""
    num_players: int = 2

    def setup(self, g) -> None:
        raise NotImplementedError

    def first_frame(self, g) -> Frame:
        raise NotImplementedError

    def on_death(self, g, seat: int, killer: Optional[int]) -> None:
        pass

    def draw_phase_count(self, g, seat: int, base: int) -> int:
        return base

    def is_enemy(self, g, a: int, b: int) -> bool:
        return a != b

    def known_enemy(self, g, viewer: int, other: int) -> bool:
        """viewer 视角下已知的敌我关系（身份局里不能直接用 is_enemy，因为身份是隐藏的）。"""
        return self.is_enemy(g, viewer, other)
