"""sgs：可扩展、可 rollout 的三国杀规则引擎。"""
from .core.game import Game  # noqa: F401
from .core.registry import CARDS, GENERALS, MODES, SKILLS, load_all  # noqa: F401
from .env import SgsEnv  # noqa: F401

__version__ = "0.1.0"
