"""提示词的两个可插拔选项：武将技巧（tips）和思维链（cot）。都不开时完全不起作用，提示词和原来一字不差。

    players:
      A:
        include: models/step-5-preview.yaml
        prompt: v2
        tips: true                 # 注入自己武将的技巧（configs/tips/武将名.md）
        cot: key                   # off（默认）/ key（只在关键决策先写三行短分析）/ all（每个决策都写）
        cot_max_chars: 80          # 三行分析加起来最多多少字（去掉空白后的字数，含“局面：”这些标签）

不常用的（都有默认值，一般不用写）：
    tips:                      # 写成一段时：对手武将的技巧也注入（默认不注入）
      dir: configs/tips
      self: true
      opponent: true
    cot_retries: 1             # 分析的回复解析不出编号（多半是被截断）时重答几次，再不行交给兜底 AI
    cot_max_tokens: null       # 需要分析的请求单独的 max_tokens；auto = 字数上限 × 2 + 60（推理模型别设小）
    cot_source: content        # content：回答里先写三行再给编号（默认，也是推荐用法）；
                               # reasoning：实验用，提示词不变，把推理模型的思考字段原文记下来，导出时再压缩

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

COT_SOURCES = ("content", "reasoning")
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
DEFAULT_MAX_CHARS = 80
COT_LABELS = ("局面", "得失", "结论")
COT_EXAMPLE = ("局面：对面 1 血没有手牌，我有杀和过河拆桥\n"
               "得失：直接杀他没闪可出；先拆没有必要\n"
               "结论：直接杀")


def auto_max_tokens(max_chars: int) -> int:
    """需要分析的请求的 max_tokens：中文大约 1～1.5 个 token 一个字，再留出 JSON 和换行的余量。"""
    return int(max_chars) * 2 + 60


def cot_format(max_chars: int) -> str:
    return (f"先分析，再给编号。分析固定写三行，三行加起来不超过 {max_chars} 字，每行一句话：\n"
            "局面：最关键的局面信息\n得失：几个选项各自的主要得失\n结论：选哪个\n"
            '然后最后一行只写 JSON：{"action": 编号}\n'
            f"格式示例（内容和这道题无关）：\n{COT_EXAMPLE}\n" + '{"action": 0}')


def cot_system_note(mode: str, max_chars: int) -> str:
    if mode == "all":
        return f"回答格式：每个决定都先写三行简短分析（局面、得失、结论，共不超过 {max_chars} 字），最后一行写 JSON，具体见每道题末尾。"
    return "回答格式：按每道题末尾写的格式回答。关键的决定，题目末尾会要求你先写三行简短分析再给编号；其它决定直接给编号。"


def analysis_len(s: Optional[str]) -> int:
    """分析的字数：去掉空白后的字符数（含“局面：”这些标签）。"""
    return len(re.sub(r"\s", "", s or ""))


def three_lines(s: Optional[str]) -> bool:
    """是不是正好三行、依次以 局面 / 得失 / 结论 开头。"""
    lines = [x.strip() for x in (s or "").splitlines() if x.strip()]
    return len(lines) == 3 and all(re.match(rf"^{lb}\s*[:：]", x) for lb, x in zip(COT_LABELS, lines))


def visible_text(text: Optional[str]) -> str:
    """去掉推理模型写在正文里的 <think>…</think>；没写完的思考（被截断）整段去掉。"""
    t = THINK.sub("", text or "")
    low = t.lower()
    if "<think>" in low:
        t = t[:low.index("<think>")]
    return t.strip()


def parse_cot(text: Optional[str], n: int, labels: Sequence[str] = ()) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """返回 (编号, 错误原因, 分析文本)。编号只从最后一个带 action 的 JSON 取；没有 JSON 时看最后一行。
    不在分析正文里找数字，避免把“选 2 不好”里的 2 当成答案。推理模型的内部思考不算分析。"""
    if text is None:
        return None, "回复为空", None
    t = visible_text(text)
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
    return None, "没有在最后一行找到 {\"action\": 编号}", _clean_analysis(t)


def _clean_analysis(s: str) -> Optional[str]:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.I).strip()
    s = re.sub(r"^分析\s*[:：]\s*", "", s)
    s = "\n".join(x.strip() for x in s.splitlines() if x.strip())
    return s or None


# ---------------------------------------------------------------------------- 包装器
class TipsCoTPrompt:
    """包在一个 PromptBuilder 外面。LLMAgent 用到的 messages() / retry() / parse() 都有。"""

    def __init__(self, inner: PromptBuilder, tips: Optional[Tips] = None, cot: str = "off",
                 cot_max_chars: int = DEFAULT_MAX_CHARS, cot_key: Union[None, str, Callable] = None,
                 cot_max_tokens: Any = None, cot_retries: int = 1, cot_source: str = "content"):
        if cot not in COT_MODES:
            raise ValueError(f"cot 只能是 {' / '.join(COT_MODES)}，现在是 {cot!r}")
        self.inner = inner
        self.tips = tips
        self.cot = cot
        self.cot_max_chars = int(cot_max_chars)
        self.cot_max_tokens = auto_max_tokens(self.cot_max_chars) if cot_max_tokens == "auto" else (
            None if cot_max_tokens in (None, "none", "null", 0) else int(cot_max_tokens))
        self.cot_retries = int(cot_retries)
        if cot_source not in COT_SOURCES:
            raise ValueError(f"cot_source 只能是 {' / '.join(COT_SOURCES)}，现在是 {cot_source!r}")
        self.cot_source = cot_source
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

    @property
    def last_cot_prompt(self) -> bool:
        """这次的提示词是不是要求“先写三行分析”（cot_source: reasoning 时提示词不变，只是把思考字段记下来）。"""
        return self.last_cot and self.cot_source == "content"

    @property
    def last_tips(self) -> Optional[str]:
        """上一次注入的技巧段落原文（导出训练数据时想去掉技巧，就把它从 system 里删掉）。"""
        return getattr(self._local, "tips", None)

    def messages(self, view, d) -> List[Dict[str, str]]:
        msgs = [dict(m) for m in self.inner.messages(view, d)]
        use = self.wants_cot(view, d)
        self._local.cot = use
        sys_m = next((m for m in msgs if m["role"] == "system"), None)
        if sys_m is not None:
            body, fmt = _split_format(sys_m["content"], "\n")
            extra = []
            self._local.tips = None
            if self.tips is not None:
                sec = self.tips.section(view)
                if sec:
                    extra.append(sec)
                    self._local.tips = sec
            if self.cot != "off" and self.cot_source == "content":
                fmt = cot_system_note(self.cot, self.cot_max_chars)
            sys_m["content"] = "\n".join([body] + (["", *extra, ""] if extra else []) + ([fmt] if fmt else []))
        if use and self.cot_source == "content":
            _cot_user(msgs, self.cot, self.cot_max_chars)
        return msgs

    def retry(self, error: str, n: int) -> Dict[str, str]:
        if not self.last_cot_prompt:
            return self.inner.retry(error, n)
        return {"role": "user", "content": f"你的回答没法解析：{error}。分析写短一点，三行加起来不超过 "
                                           f"{self.cot_max_chars} 字，最后一行一定要单独写 {{\"action\": 编号}}，"
                                           f"编号从 0 到 {n - 1}。"}

    def parse(self, text: Optional[str], n: int, labels: Sequence[str] = ()):
        if self.last_cot_prompt:
            return parse_cot(text, n, labels)
        return parse_action(text, n, labels)

    def describe(self) -> Dict[str, Any]:
        return {"tips": None if self.tips is None else {"dir": self.tips.dir, "self": self.tips.self_,
                                                         "opponent": self.tips.opponent},
                "cot": self.cot, "cot_source": self.cot_source, "cot_max_chars": self.cot_max_chars,
                "cot_max_tokens": self.cot_max_tokens}


def _cot_user(msgs: List[Dict[str, str]], cot: str, max_chars: int) -> None:
    u = next((m for m in reversed(msgs) if m["role"] == "user"), None)
    if u is not None:
        body, _ = _split_format(u["content"], "\n\n")
        u["content"] = body + "\n\n" + ("这是一个关键决定。" if cot == "key" else "") + cot_format(max_chars)


def to_cot_messages(msgs: List[Dict[str, str]], cot: str = "key", max_chars: int = DEFAULT_MAX_CHARS,
                    drop_tips: Optional[str] = None) -> List[Dict[str, str]]:
    """把一次“只要编号”的提示词改写成“先写三行分析再给编号”的样子（导出训练数据用：
    cot_source: reasoning 跑出来的对局，提示词里没有三行格式，训练时要换成和推理时一样的格式）。
    drop_tips 给技巧段落原文时顺便把它去掉。已经是三行格式的原样返回（只去技巧）。"""
    out = [dict(m) for m in msgs]
    sys_m = next((m for m in out if m["role"] == "system"), None)
    if sys_m is not None:
        c = sys_m["content"]
        if drop_tips and drop_tips in c:
            c = c.replace("\n\n" + drop_tips + "\n\n", "\n").replace(drop_tips, "")
        body, fmt = _split_format(c, "\n")
        if fmt is not None:
            c = body + "\n" + cot_system_note(cot, max_chars)
        sys_m["content"] = c
    u = next((m for m in reversed(out) if m["role"] == "user"), None)
    if u is not None and "先分析，再给编号" not in u["content"]:
        _cot_user(out, cot, max_chars)
    return out


def _split_format(content: str, sep: str) -> Tuple[str, Optional[str]]:
    """把末尾的“只输出一行 JSON……”拆出来。不是内置格式（你自己的模板）就原样返回、格式为 None。"""
    for f in (FORMAT_REASON, FORMAT_PLAIN):
        if content.endswith(sep + f):
            return content[: -len(sep + f)], f
        if content.endswith(f):
            return content[: -len(f)].rstrip("\n"), f
    return content, None


def wrap_prompt(inner: PromptBuilder, tips: Any = None, cot: str = "off", cot_max_chars: int = DEFAULT_MAX_CHARS,
                cot_key: Any = None, cot_max_tokens: Any = None, cot_retries: int = 1, cot_source: str = "content"):
    """tips 和 cot 都没开就原样返回 inner（提示词和行为跟原来完全一样）。"""
    cot = "off" if cot in (None, False, "") else ("all" if cot is True else str(cot))
    t = Tips.from_config(tips)
    if t is None and cot == "off":
        return inner
    return TipsCoTPrompt(inner, t, cot, cot_max_chars, cot_key, cot_max_tokens, cot_retries, cot_source)
