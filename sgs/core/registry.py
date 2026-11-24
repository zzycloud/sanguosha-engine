"""插件注册表：卡牌、技能、武将、模式、牌堆。

开闭原则的落点：核心引擎只通过这些注册表按名字查找规则对象，
新增武将/技能/卡牌/模式只需要在新模块里注册，不改核心代码。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple, TypeVar

T = TypeVar("T")

CARDS: Dict[str, "object"] = {}      # 牌名 -> CardSpec 实例
SKILLS: Dict[str, "object"] = {}     # 技能名 -> Skill 实例
GENERALS: Dict[str, "General"] = {}  # 武将名 -> General
MODES: Dict[str, type] = {}          # 模式名 -> Mode 类
DECKS: Dict[str, Callable[[], List[Tuple[str, str, int]]]] = {}  # 牌堆名 -> 生成 (牌名, 花色, 点数) 列表


class General:
    __slots__ = ("name", "kingdom", "hp", "gender", "skills", "pack", "lord_skills")

    def __init__(self, name: str, kingdom: str, hp: int, gender: str, skills: Tuple[str, ...],
                 pack: str = "standard", lord_skills: Tuple[str, ...] = ()):
        self.name = name
        self.kingdom = kingdom
        self.hp = hp
        self.gender = gender  # "male" / "female"
        self.skills = tuple(skills)
        self.pack = pack
        self.lord_skills = tuple(lord_skills)

    def __repr__(self) -> str:
        return f"General({self.name})"


def _unique(table: dict, key: str, kind: str) -> None:
    if key in table:
        raise ValueError(f"{kind} {key!r} 重复注册")


def register_card(cls: type) -> type:
    """装饰器：注册一种卡牌（按 cls.name）。"""
    _unique(CARDS, cls.name, "卡牌")
    CARDS[cls.name] = cls()
    return cls


def register_skill(cls: type) -> type:
    """装饰器：注册一个技能（按 cls.name）。"""
    _unique(SKILLS, cls.name, "技能")
    SKILLS[cls.name] = cls()
    return cls


def register_general(name: str, kingdom: str, hp: int, gender: str, skills, pack: str = "standard",
                     lord_skills=()) -> General:
    _unique(GENERALS, name, "武将")
    for s in tuple(skills) + tuple(lord_skills):
        if s not in SKILLS:
            raise ValueError(f"武将 {name} 的技能 {s!r} 还没注册")
    g = General(name, kingdom, hp, gender, tuple(skills), pack, tuple(lord_skills))
    GENERALS[name] = g
    return g


def register_mode(cls: type) -> type:
    _unique(MODES, cls.name, "模式")
    MODES[cls.name] = cls
    return cls


def register_deck(name: str):
    def deco(fn):
        _unique(DECKS, name, "牌堆")
        DECKS[name] = fn
        return fn
    return deco


def generals_in(pack: Optional[str] = None) -> List[str]:
    return [n for n, g in GENERALS.items() if pack is None or g.pack == pack]


_LOADED = False


def load_all() -> None:
    """导入内置扩展包。第三方包可以自己 import 后调用 register_*。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from .. import packs  # noqa: F401  触发各扩展包注册
    from .. import modes  # noqa: F401
