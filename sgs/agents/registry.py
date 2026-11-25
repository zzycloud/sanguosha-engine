"""智能体注册表：用一个字符串描述“这个座位由谁来打”。

    make_agent("rule")                                         # 规则 AI
    make_agent("random")                                       # 随机 AI
    make_agent("llm?model=qwen2.5-7b&base_url=http://localhost:8000/v1")
    make_agent("llm@configs/my_model.json")                    # 配置写在 JSON 文件里
    make_agent("llm@configs/my_model.json?temperature=0.2")    # 文件 + 覆盖
    make_agent("mypkg.bots:MyAgent?depth=3")                   # 不改本仓库，直接用你自己的类

新增一种智能体不需要改这里或命令行：在你的模块里

    @register_agent("mcts")
    class MCTSAgent(Agent): ...

然后 import 一下这个模块（或者直接用 "模块:类名" 的写法）。
"""
from __future__ import annotations

import importlib
import json
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qsl

from .base import Agent

AGENTS: Dict[str, Callable[..., Agent]] = {}


def register_agent(name: str):
    def deco(factory):
        AGENTS[name] = factory
        return factory
    return deco


def _coerce(v: str) -> Any:
    """命令行里的 key=value：数字、true/false/null 和 JSON 自动转换，其它保持字符串。"""
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def parse_spec(spec: str) -> Tuple[str, Dict[str, Any]]:
    """'kind[@config.json][?k=v&k=v]' -> (kind, 配置字典)。"""
    spec = spec.strip()
    query = ""
    if "?" in spec:
        spec, query = spec.split("?", 1)
    path = None
    if "@" in spec:
        spec, path = spec.split("@", 1)
    cfg: Dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    for k, v in parse_qsl(query, keep_blank_values=True):
        cfg[k] = _coerce(v)
    kind = spec or cfg.pop("kind", "")
    cfg.pop("kind", None)
    return kind, cfg


def _resolve(kind: str) -> Callable[..., Agent]:
    if kind in AGENTS:
        return AGENTS[kind]
    if ":" in kind:  # "包.模块:类名"
        mod, _, attr = kind.partition(":")
        return getattr(importlib.import_module(mod), attr)
    raise KeyError(f"未知的智能体 {kind!r}；已注册：{', '.join(sorted(AGENTS))}，或写成 '模块:类名'")


def make_agent(spec: Any, seed: Optional[int] = None, **overrides) -> Agent:
    """spec 可以是字符串（见模块说明）、{"kind": ..., ...} 字典，或已经建好的 Agent 实例（原样返回）。
    seed 会作为 seed=... 传给构造函数（构造函数不收 seed 时自动省略）。"""
    if isinstance(spec, Agent):
        return spec
    if isinstance(spec, dict):
        cfg = dict(spec)
        kind = cfg.pop("kind")
    else:
        kind, cfg = parse_spec(str(spec))
    cfg.update(overrides)
    factory = _resolve(kind)
    if seed is not None and "seed" not in cfg:
        try:
            return factory(seed=seed, **cfg)
        except TypeError as e:
            if "seed" not in str(e):
                raise
    return factory(**cfg)


def spec_name(spec: Any) -> str:
    """记录用的简短名字（不含 api_key 等配置）。"""
    if isinstance(spec, Agent):
        return getattr(spec, "label", None) or spec.name
    if isinstance(spec, dict):
        k = spec.get("kind", "?")
        return f"{k}:{spec['model']}" if spec.get("model") else k
    kind, cfg = parse_spec(str(spec))
    return f"{kind}:{cfg['model']}" if cfg.get("model") else kind


def _builtin():
    from .rule import RuleAgent
    from .base import RandomAgent
    AGENTS.setdefault("rule", RuleAgent)
    AGENTS.setdefault("random", RandomAgent)

    def llm(**cfg):
        from .llm import LLMAgent
        return LLMAgent(**cfg)
    AGENTS.setdefault("llm", llm)


_builtin()
