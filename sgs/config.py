"""对战配置文件：一个文件写清楚“谁和谁打、打几局、怎么打、输出什么”。

支持 .yaml/.yml、.json、.toml。完整字段见 DEFAULTS 和 configs/ 里带注释的示例。

    python -m sgs.arena configs/rule-vs-random.yaml
    python -m sgs.arena configs/local-vllm.yaml --set match.games=50 --set players.A.temperature=0

命令行只做覆盖：--set 路径=值，路径用点分隔，值按 YAML 解析（数字、true/false、[列表] 都行）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

DEFAULTS: Dict[str, Any] = {
    "match": {
        "mode": "duel",          # 游戏模式（目前只有 1v1：duel）
        "games": 10,             # 局数
        "seed": 0,               # 第一局的种子，之后依次 +1（换边时每两局用同一个种子）
        "swap": True,            # 每两局用同一副牌、同一对武将，交换先后手
        "generals": "random",    # random，或者 [0 号位武将, 1 号位武将]（0 号位先手）
        "general_pool": None,    # generals 为 random 时从这里抽；不填就是全部标准武将
        "concurrency": 1,        # 同时进行的对局数
        "game_timeout": None,    # 单局最多多少秒
        "max_steps": 20000,      # 单局最多多少步
    },
    "players": {
        "A": {"agent": "rule"},
        "B": {"agent": "rule"},
    },
    "output": {
        "dir": "runs/{time}",    # 输出目录；{time} 换成当前时间，{name} 换成配置文件名
        "replays": 5,            # 录几局录像：数字、all 或 none
        "decisions": True,       # 写 decisions.jsonl（每个决策点一行）
        "llm_calls": True,       # 写 llm_calls.jsonl（模型的完整请求和回复）
        "rollouts": 0,           # 给每个决策点附 rollout 估值（很慢）
        "history": "new",        # decisions 里的 history：new 只放新日志，full 放全部
        "privileged": True,      # decisions 里带上帝视角字段
        "progress": True,        # 每局结束打印一行
    },
}

SECRET_KEYS = {"api_key", "headers"}


class ConfigError(ValueError):
    pass


def _parse_text(text: str, ext: str) -> Any:
    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:  # pragma: no cover
            raise ConfigError("读 YAML 需要 PyYAML：pip install pyyaml（或者把配置写成 .json / .toml）") from None
        return yaml.safe_load(text) or {}
    if ext == ".toml":
        try:
            import tomllib
        except ImportError:  # pragma: no cover  Python 3.11 以前
            raise ConfigError("读 TOML 需要 Python 3.11+（或者把配置写成 .yaml / .json）") from None
        return tomllib.loads(text)
    return json.loads(text)


def read_file(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = _parse_text(f.read(), os.path.splitext(path)[1].lower())
    if not isinstance(data, dict):
        raise ConfigError(f"{path}：顶层必须是键值对")
    return data


def merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：over 里的字典逐层覆盖 base，其它值直接替换。"""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_value(v: str) -> Any:
    try:
        import yaml
        return yaml.safe_load(v)
    except Exception:
        try:
            return json.loads(v)
        except ValueError:
            return v


def apply_overrides(cfg: Dict[str, Any], sets: Sequence[str]) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    for s in sets or ():
        if "=" not in s:
            raise ConfigError(f"--set 要写成 路径=值，例如 match.games=50（收到 {s!r}）")
        path, val = s.split("=", 1)
        keys = path.strip().split(".")
        node = cfg
        for k in keys[:-1]:
            node = node.setdefault(k, {})
            if not isinstance(node, dict):
                raise ConfigError(f"--set {path}：{k} 不是一个配置段")
        node[keys[-1]] = parse_value(val)
    return cfg


def _check_keys(section: str, got: Dict[str, Any], allowed: Dict[str, Any]) -> None:
    bad = [k for k in got if k not in allowed]
    if bad:
        raise ConfigError(f"{section} 里有不认识的字段：{', '.join(bad)}；可用：{', '.join(allowed)}")


def load(path: Optional[str] = None, sets: Sequence[str] = (), data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读配置文件（或直接给 data），合并默认值和 --set 覆盖，检查字段。"""
    raw: Dict[str, Any] = {}
    base_dir = os.getcwd()
    if path:
        raw = read_file(path)
        base_dir = os.path.dirname(os.path.abspath(path))
    if data:
        raw = merge(raw, data)
    raw = apply_overrides(raw, sets)
    _check_keys("配置顶层", raw, {**DEFAULTS, "name": None})
    for sec in ("match", "output"):
        _check_keys(sec, raw.get(sec) or {}, DEFAULTS[sec])
    cfg = merge({k: v for k, v in DEFAULTS.items() if k != "players"}, raw)
    players = {**copy.deepcopy(DEFAULTS["players"]), **(raw.get("players") or {})}   # 没写的一方默认规则 AI
    if set(players) != {"A", "B"}:
        raise ConfigError(f"players 下只能有 A 和 B 两项（收到 {', '.join(players)}）")
    cfg["players"] = {k: resolve_player(f"players.{k}", v, base_dir) for k, v in players.items()}
    cfg["name"] = raw.get("name") or (os.path.splitext(os.path.basename(path))[0] if path else "arena")
    cfg["_base_dir"] = base_dir
    g = cfg["match"]["generals"]
    if g != "random" and not (isinstance(g, list) and len(g) == 2):
        raise ConfigError("match.generals 写 random 或者两个武将名的列表（0 号位、1 号位），例如 [关羽, 吕布]")
    if isinstance(g, list) or cfg["match"]["general_pool"]:
        from .core.registry import GENERALS, load_all
        load_all()
        names = (g if isinstance(g, list) else []) + list(cfg["match"]["general_pool"] or [])
        bad = [n for n in names if n not in GENERALS]
        if bad:
            raise ConfigError(f"没有这些武将：{', '.join(bad)}")
    return cfg


def resolve_player(where: str, p: Any, base_dir: str) -> Dict[str, Any]:
    """players.A 可以是一个字符串（rule、random、llm?model=...）或一个配置段：
    agent 是智能体名字，其余字段原样交给智能体；include 指向另一个配置文件（相对本文件），先读它再覆盖。"""
    if isinstance(p, str):
        return {"agent": p}
    if not isinstance(p, dict):
        raise ConfigError(f"{where} 要么是字符串，要么是配置段")
    p = dict(p)
    inc = p.pop("include", None)
    if inc:
        ip = inc if os.path.isabs(inc) else os.path.join(base_dir, inc)
        p = merge(read_file(ip), p)
    if "agent" not in p:
        raise ConfigError(f"{where} 缺少 agent（rule / random / llm / 你注册的名字 / 模块:类名）")
    return p


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        def rep(m):
            if m.group(1) not in os.environ:
                raise ConfigError(f"配置里用了环境变量 {m.group(1)}，但它没有设置")
            return os.environ[m.group(1)]
        return _ENV.sub(rep, v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def agent_spec(p: Dict[str, Any]) -> Any:
    """配置段 -> make_agent 能用的写法（字符串原样；配置段换成 {"kind": ...}，并展开 ${环境变量}）。"""
    p = dict(p)
    kind = p.pop("agent")
    if not p:
        return kind
    return {"kind": kind, **_expand(p)}


def redacted(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """写进输出目录的配置副本：去掉密钥和内部字段。"""
    def red(x):
        if isinstance(x, dict):
            return {k: ("***" if k in SECRET_KEYS and v else red(v)) for k, v in x.items() if not k.startswith("_")}
        if isinstance(x, list):
            return [red(v) for v in x]
        return x
    return red(cfg)


def out_dir(cfg: Dict[str, Any]) -> Optional[str]:
    d = cfg["output"].get("dir")
    if not d:
        return None
    return d.replace("{time}", time.strftime("%Y%m%d-%H%M%S")).replace("{name}", cfg["name"])


def dump_yaml(cfg: Dict[str, Any]) -> str:
    try:
        import yaml
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except ImportError:  # pragma: no cover
        return json.dumps(cfg, ensure_ascii=False, indent=2)


def generals_list(cfg: Dict[str, Any]) -> Optional[List[str]]:
    g = cfg["match"]["generals"]
    return None if g == "random" else list(g)
