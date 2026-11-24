"""结算栈的基本单元：Frame、Decision、Option、事件对象。

整个对局是一个显式的结算栈（list[Frame]）。每个 Frame 是一个小状态机，
用 self.stage 记录走到哪一步。引擎每次取栈顶调用 step()：
  - 需要子结算时 g.push(child) 后返回，子结算结束后再次 step 时用 g.ret 取结果；
  - 需要玩家决策时 g.ask(decision) 后返回，下次 step 时用 g.answer() 取选项；
  - 结束时 g.done(result)。
因为没有 Python 调用栈/生成器，整个对局状态可以 pickle/深拷贝，
在任意决策点复制出来做 rollout。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class Frame:
    stage: int = 0

    def step(self, g) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}(stage={self.stage})"


class Option:
    """一个可选动作。key 是只含基本类型的元组，适合做 RL 动作编码；label 给人和 LLM 看。"""

    __slots__ = ("key", "label", "data")

    def __init__(self, key: Tuple, label: str, data: Any = None):
        self.key = key
        self.label = label
        self.data = data

    def __repr__(self) -> str:
        return f"Option({self.label})"


class Decision:
    """一次需要某个玩家做的选择。

    kind 常见取值：
      play      出牌阶段选一张牌/技能/结束
      respond   被要求使用或打出某种牌（闪、杀、桃、无懈可击）
      trigger   是否发动某个技能
      choose    其它单选（选牌、选目标、选择分支）
    """

    __slots__ = ("player", "kind", "prompt", "options", "ctx")

    def __init__(self, player: int, kind: str, prompt: str, options: List[Option],
                 ctx: Optional[Dict[str, Any]] = None):
        self.player = player
        self.kind = kind
        self.prompt = prompt
        self.options = options
        self.ctx = ctx or {}

    def __repr__(self) -> str:
        return f"Decision(p{self.player} {self.kind}: {self.prompt} [{len(self.options)}])"


class Ev:
    """事件数据。技能通过修改其字段影响结算（例如伤害值、需要的闪数量）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self) -> str:
        return f"Ev({self.__dict__})"


YES = Option(("yes",), "是")
NO = Option(("no",), "否")


def yes_no(player: int, prompt: str, ctx=None) -> Decision:
    return Decision(player, "trigger", prompt, [Option(("yes",), "是"), Option(("no",), "否")], ctx)
