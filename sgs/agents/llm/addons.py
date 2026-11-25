"""提示词的两个可插拔选项：武将技巧（tips）和思维链（cot）。都不开时完全不起作用，提示词和原来一字不差。

    players:
      A:
        include: models/strong-api.yaml
        prompt: v2
        tips: true                 # 注入自己武将的技巧（configs/tips/武将名.md）
        # tips: {dir: configs/tips, self: true, opponent: true}   # 对手武将的技巧也注入（默认不注入）
        cot: key                   # off（默认）/ key（只在关键决策先分析）/ all（每个决策都先分析）
        cot_max_chars: 150         # 分析最多写多少字（写进提示词的要求，不硬截断）
        max_tokens: 1024           # 开思维链时回复变长，max_tokens 要留够

实现方式：TipsCoTPrompt 包在任何一个提示词模板（default、v2 或你自己的）外面，只改三处：
  1. system 末尾的输出格式说明前面，插入技巧段落；
  2. 需要分析的决策，把题目末尾的“只输出一行 JSON”换成“先写分析，最后一行写 JSON”；
  3. 解析回复时，分析文本单独拿出来（记进 llm_calls.json 的 analysis 字段，录像里能看到），编号从最后的 JSON 里取。
解析不出编号时和原来一样：告诉模型重答（max_retries 次），还不行交给兜底 AI，并计入“答错”和“兜底”次数。

哪些算关键决策（cot: key）见 is_key_decision；想换规则，写 cot_key: 你的模块:函数名（参数 view, d，返回 True/False）。
"""
from __future__ import annotations

import importlib
import json
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .parse import JSON_OBJ, THINK, parse_action
from .prompt import FORMAT_PLAIN, FORMAT_REASON, PromptBuilder

COT_MODES = ("off", "key", "all")

# 受到伤害后能发动的技能：被杀时“出闪还是扣血发技能”是真正要想的（卖血）
SELL_HP_SKILLS = {"遗计", "反馈", "奸雄", "刚烈"}
# 选对方的牌：过河拆桥 / 顺手牵羊拆哪张、拿哪张
PICK_REASONS = {"过河拆桥", "顺手牵羊"}

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEFAULT_TIPS_DIR = os.path.join("configs", "tips")


# ---------------------------------------------------------------------------- 关键决策
def is_key_decision(view, d) -> bool:
    """默认规则（cot: key 时只在这些决策上先分析）：
    - 是否发动技能（trigger），以及技能里面的选择（观星怎么放、遗计给谁、反馈拿哪张……）；
    - 过河拆桥、顺手牵羊、贯石斧、雌雄双股剑这类“选哪张牌”；
    - 弃牌（弃牌阶段或被要求弃牌）；
    - 卖血候选：自己有遗计/反馈/奸雄/刚烈，被杀时手里有闪可以出，也可以不出。
    出牌阶段出什么、普通的出不出闪、无懈可击，都不算（大多数一眼能看出来）。"""
    ctx = d.ctx or {}
    if ctx.get("forced"):
        return False
    if d.kind == "trigger" or ctx.get("skill"):
        return True
    reason = ctx.get("reason") or ""
    if reason in PICK_REASONS or reason in ("discard", "discard_phase"):
        return True
    if reason:
        from ...core import registry as R
        if reason in R.SKILLS or reason in ("贯石斧", "雌雄双股剑"):
            return True
    if d.kind == "respond" and "闪" in (ctx.get("names") or ()) and ctx.get("slash"):
        me = view.players[view.viewer]
        if SELL_HP_SKILLS & set(me.skills or ()) and len(d.options) >= 2:
            return True
    return False


def _load_fn(spec: Union[None, str, Callable]) -> Callable:
    if spec is None:
        return is_key_decision
    if callable(spec):
        return spec
    mod, _, attr = str(spec).partition(":")
    if not attr:
        raise ValueError(f"cot_key 要写成 模块:函数名，现在是 {spec!r}")
    return getattr(importlib.import_module(mod), attr)


# ---------------------------------------------------------------------------- 技巧
_COMMENT = re.compile(r"<!--.*?-->", re.S)


class Tips:
    """从目录里读“武将名.md”。文件里的 <!-- 注释 --> 不会进提示词。没有这个武将的文件就不注入。"""

    def __init__(self, dir: Optional[str] = None, self_: bool = True, opponent: bool = False):
        self.dir = self._find_dir(dir or DEFAULT_TIPS_DIR)
        self.self_ = self_
        self.opponent = opponent
        self._cache: Dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _find_dir(d: str) -> str:
        if os.path.isabs(d):
            return d
        for base in (os.getcwd(), REPO_ROOT):       # 相对路径：先按当前目录找，再按仓库根目录找
            p = os.path.join(base, d)
            if os.path.isdir(p):
                return p
        raise FileNotFoundError(f"找不到技巧目录 {d}（试过当前目录和仓库根目录）")

    def text(self, general: str) -> Optional[str]:
        with self._lock:
            if general not in self._cache:
                p = os.path.join(self.dir, f"{general}.md")
                t = None
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        t = _COMMENT.sub("", f.read()).strip() or None
                self._cache[general] = t
            return self._cache[general]

    def section(self, view) -> str:
        me = view.players[view.viewer]
        out: List[str] = []
        if self.self_:
            t = self.text(me.general)
            if t:
                out.append(f"【你的武将 {me.general} 的实战技巧】（玩家经验，只是参考：要结合当前局面判断，"
                           f"和局面冲突时以局面为准，不要生搬硬套）\n{t}")
        if self.opponent:
            for p in view.players:
                if p.seat != view.viewer and view.is_enemy(p.seat):
                    t = self.text(p.general)
                    if t:
                        out.append(f"【对手武将 {p.general} 的常见打法】（玩家经验，只是参考，可以用来预判对手）\n{t}")
        return "\n\n".join(out)

    @classmethod
    def from_config(cls, cfg: Any) -> Optional["Tips"]:
        if cfg in (None, False, "off", "none", ""):
            return None
        if cfg is True or cfg == "on":
            return cls()
        if isinstance(cfg, str):
            return cls(dir=cfg)
        if isinstance(cfg, dict):
            bad = set(cfg) - {"dir", "self", "opponent"}
            if bad:
                raise TypeError(f"tips 不认识这些配置：{', '.join(sorted(bad))}（可用 dir / self / opponent）")
            return cls(dir=cfg.get("dir"), self_=bool(cfg.get("self", True)), opponent=bool(cfg.get("opponent", False)))
        raise TypeError(f"tips 要写成 true / 目录 / {{dir, self, opponent}}，现在是 {cfg!r}")


# ---------------------------------------------------------------------------- 思维链格式和解析
def cot_format(max_chars: int) -> str:
    return (f"先写“分析：”，用不超过 {max_chars} 字分析局面要点和几个选项的得失；"
            '然后另起一行，最后一行只写 JSON：{"action": 编号}')


def cot_system_note(mode: str, max_chars: int) -> str:
    if mode == "all":
        return "回答格式：" + cot_format(max_chars)
    return "回答格式：按每道题末尾写的格式回答。关键的决定，题目末尾会要求你先写分析再给编号；其它决定直接给编号。"


def parse_cot(text: Optional[str], n: int, labels: Sequence[str] = ()) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """返回 (编号, 错误原因, 分析文本)。编号只从最后一个带 action 的 JSON 取；没有 JSON 时看最后一行。
    不在分析正文里找数字，避免把“选 2 不好”里的 2 当成答案。"""
    if text is None:
        return None, "回复为空", None
    t = THINK.sub("", text).strip()
    objs = [(m.start(), m.end(), m.group(0)) for m in JSON_OBJ.finditer(t)]
    for s, e, raw in reversed(objs):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            i, err, _ = parse_action(json.dumps({"action": obj["action"]}), n, labels)
            return i, err, _clean_analysis(t[:s]) or (obj.get("analysis") if isinstance(obj.get("analysis"), str) else None)
    lines = [x for x in t.splitlines() if x.strip()]
    if not lines:
        return None, "回复为空（可能被 max_tokens 截断）", None
    i, err, _ = parse_action(lines[-1], n, labels)
    if err is None:
        return i, None, _clean_analysis("\n".join(lines[:-1]))
    return None, "没有在最后一行找到 {\"action\": 编号}" + ("（分析可能写太长被截断了）" if len(t) > 50 else ""), \
        _clean_analysis(t)


def _clean_analysis(s: str) -> Optional[str]:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.I).strip()
    s = re.sub(r"^分析\s*[:：]\s*", "", s)
    return s or None


# ---------------------------------------------------------------------------- 包装器
class TipsCoTPrompt:
    """包在一个 PromptBuilder 外面。LLMAgent 用到的 messages() / retry() / parse() 都有。"""

    def __init__(self, inner: PromptBuilder, tips: Optional[Tips] = None, cot: str = "off",
                 cot_max_chars: int = 150, cot_key: Union[None, str, Callable] = None):
        if cot not in COT_MODES:
            raise ValueError(f"cot 只能是 {' / '.join(COT_MODES)}，现在是 {cot!r}")
        self.inner = inner
        self.tips = tips
        self.cot = cot
        self.cot_max_chars = int(cot_max_chars)
        self.key_fn = _load_fn(cot_key)
        self._local = threading.local()

    def __getattr__(self, name):            # 其它属性（ask_reason 等）照旧取里面那个模板的
        return getattr(self.inner, name)

    def wants_cot(self, view, d) -> bool:
        if self.cot == "all":
            return True
        if self.cot == "key":
            return bool(self.key_fn(view, d))
        return False

    @property
    def last_cot(self) -> bool:
        return getattr(self._local, "cot", False)

    def messages(self, view, d) -> List[Dict[str, str]]:
        msgs = [dict(m) for m in self.inner.messages(view, d)]
        use = self.wants_cot(view, d)
        self._local.cot = use
        sys_m = next((m for m in msgs if m["role"] == "system"), None)
        if sys_m is not None:
            body, fmt = _split_format(sys_m["content"], "\n")
            extra = []
            if self.tips is not None:
                sec = self.tips.section(view)
                if sec:
                    extra.append(sec)
            if self.cot != "off":
                fmt = cot_system_note(self.cot, self.cot_max_chars)
            sys_m["content"] = "\n".join([body] + (["", *extra, ""] if extra else []) + ([fmt] if fmt else []))
        if use:
            u = next((m for m in reversed(msgs) if m["role"] == "user"), None)
            if u is not None:
                body, _ = _split_format(u["content"], "\n\n")
                u["content"] = body + "\n\n这是一个关键决定。" + cot_format(self.cot_max_chars)
        return msgs

    def retry(self, error: str, n: int) -> Dict[str, str]:
        if not self.last_cot:
            return self.inner.retry(error, n)
        return {"role": "user", "content": f"你的回答没法解析：{error}。分析可以简短一点，"
                                           f"最后一行一定要单独写 {{\"action\": 编号}}，编号从 0 到 {n - 1}。"}

    def parse(self, text: Optional[str], n: int, labels: Sequence[str] = ()):
        if self.last_cot:
            return parse_cot(text, n, labels)
        return parse_action(text, n, labels)

    def describe(self) -> Dict[str, Any]:
        return {"tips": None if self.tips is None else {"dir": self.tips.dir, "self": self.tips.self_,
                                                         "opponent": self.tips.opponent},
                "cot": self.cot, "cot_max_chars": self.cot_max_chars}


def _split_format(content: str, sep: str) -> Tuple[str, Optional[str]]:
    """把末尾的“只输出一行 JSON……”拆出来。不是内置格式（你自己的模板）就原样返回、格式为 None。"""
    for f in (FORMAT_REASON, FORMAT_PLAIN):
        if content.endswith(sep + f):
            return content[: -len(sep + f)], f
        if content.endswith(f):
            return content[: -len(f)].rstrip("\n"), f
    return content, None


def wrap_prompt(inner: PromptBuilder, tips: Any = None, cot: str = "off", cot_max_chars: int = 150,
                cot_key: Any = None):
    """tips 和 cot 都没开就原样返回 inner（提示词和行为跟原来完全一样）。"""
    cot = "off" if cot in (None, False, "") else ("all" if cot is True else str(cot))
    t = Tips.from_config(tips)
    if t is None and cot == "off":
        return inner
    return TipsCoTPrompt(inner, t, cot, cot_max_chars, cot_key)
