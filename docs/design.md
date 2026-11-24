# 整体设计

这个引擎的主要用途是给 LLM / RL 跑大量对局、在任意决策点复制状态做 rollout。所以设计上优先保证三件事：

1. **状态可拷贝**：任意时刻 `g.clone()` 得到一份完全独立、随机数也一致的对局。
2. **可扩展（开闭原则）**：加卡牌、武将、技能、模式都只需要新写模块并注册，不改核心代码。
3. **规则按原版**：标准版规则，结算顺序参考规则书和无名杀的实现，关键结算有单测。

## 目录结构

```
sgs/
  core/            核心：不认识任何具体武将
    cards.py       实体牌 CardInfo（不可变，用 id 引用）和虚拟牌 VCard（转化牌）
    base.py        Frame / Decision / Option / Ev
    plugin.py      扩展点基类：CardSpec、Skill、Mode
    registry.py    注册表：CARDS、SKILLS、GENERALS、MODES、DECKS
    game.py        Game：区域、牌的移动、距离/攻击范围等规则查询、观察、拷贝
    actions.py     通用结算帧：回合、出牌、使用牌、无懈链、响应、伤害、濒死、判定……
  packs/standard/  标准版扩展包：牌堆、基本牌、锦囊、装备、25 名武将的技能
  modes/duel.py    1v1 两人对决模式
  agents/          随机 AI、规则 AI
  env.py           类 gym 接口
  replay.py        录像（JSON / 自带查看器的单文件 HTML）
  tools/           测速、本地网页对战
  testing.py       写单测用的局面构造工具
web/index.html     观察页面
```

## 结算栈：为什么不用生成器

三国杀的结算是深度嵌套的：杀 → 要闪 → 八卦阵判定 → 鬼才改判 → …，中间随时要停下来等某个玩家选择。
最自然的写法是协程/生成器，但 Python 的生成器不能 pickle，也就没法在决策点复制状态。

所以整局是一个**显式的结算栈** `g.stack: list[Frame]`。每个 Frame 是一个小状态机，用 `self.stage` 记录走到哪一步：

```python
class DuelFrame(Frame):
    def step(self, g):
        if self.stage == 1:
            self.stage = 2
            g.push(AskForCardFrame(self.cur, ("杀",), ...))   # 子结算
        elif self.stage == 2:
            if g.ret is None: ...                           # 子结算的返回值
```

引擎循环只做一件事：取栈顶调用 `step()`，直到出现待决策（`g.pending`）或游戏结束。

- `g.push(frame)`：压入子结算，返回后下次 step 用 `g.ret` 取结果；
- `g.ask(decision)`：请求玩家决策，`g.act(i)` 之后下次 step 用 `g.answer()` 取选项；
- `g.done(result)`：结束当前帧。

状态里只有列表、字典、整数和这些小对象，没有闭包和 lambda，`pickle` 一次中盘状态约 250 微秒。

## 事件与触发

技能通过监听事件生效。核心在以下时机调用 `trigger(g, 事件名, ev)`，`ev` 是可修改的事件对象：

| 事件 | 时机 | 典型技能 / 装备 |
| --- | --- | --- |
| `turn_start` | 回合开始 | |
| `phase_prepare` | 准备阶段 | 观星、洛神 |
| `draw_phase` | 摸牌前，`ev.count` 可改 | 英姿、裸衣、突袭 |
| `phase_discard` | 弃牌阶段开始，`ev.skip` 可加 | 克己 |
| `phase_finish` | 结束阶段 | 闭月 |
| `card_used` / `card_played` | 使用 / 打出一张牌时 | 集智 |
| `target_confirm` | 指定目标后，可改 `ev.info`（需要几张闪、不能闪、无视防具、无效） | 无双、铁骑、青釭剑、仁王盾、雌雄双股剑、流离 |
| `slash_dodged` | 杀被闪抵消后，可设 `ev.hit` | 青龙偃月刀、贯石斧 |
| `damage_cause` | 造成伤害时（来源），可改伤害或防止 | 裸衣、寒冰剑、麒麟弓 |
| `damage_inflict` | 受到伤害时（目标） | （留给扩展） |
| `damaged` / `damage_done` | 受到 / 造成伤害后 | 刚烈、反馈、遗计、奸雄 |
| `dying` / `death` | 濒死 / 阵亡 | |
| `judge_retrial` | 判定牌生效前，可改 `ev.card` | 鬼才 |
| `judge_done` | 判定牌生效后 | 天妒 |
| `lose_card` | 失去牌后（本步结束后统一触发） | 连营、枭姬 |

触发顺序：从当前回合角色开始按座次，每名角色按技能列表顺序。非锁定技先问“是否发动”；
锁定技直接发动；`self_ask=True` 的技能不先问，由技能自己的决策给出“不发动”选项（避免问两次，例如鬼才、青龙偃月刀）。

除了事件，技能还可以实现这些**规则钩子**（核心查询时汇总所有存活角色的技能）：

- `view_as`：转化（武圣、龙胆、倾国、奇袭、国色、急救、丈八蛇矛）
- `response_options` / `response_frame`：响应时的特殊选项（八卦阵）
- `can_activate` / `activate`：出牌阶段主动技（制衡、苦肉、反间、青囊、仁德、结姻、离间）
- `distance_mod`、`range_mod`、`slash_limit_mod`、`max_hand_mod`、`prohibit`、`ignore_distance`（马术、诸葛连弩、咆哮、空城、谦逊、奇才）

**装备效果也是技能**：装备牌的 `CardSpec.skill` 指向一个技能名，装上即拥有，失去即失去。所以装备和武将技能走同一套机制。

## 扩展：开闭原则怎么落地

新增内容都是“写新模块 + 注册”，核心不动。

**加一个技能和武将**（例如风林火山的夏侯渊“神速”可以照此写）：

```python
from sgs.core.plugin import Skill
from sgs.core.registry import register_skill, register_general

@register_skill
class Biyue(Skill):
    name = "闭月"
    events = ("phase_finish",)
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner
    def on_trigger(self, g, owner, event, ev):
        g.draw(owner, 1, "闭月")

register_general("貂蝉", "群", 3, "female", ("离间", "闭月"), pack="standard")
```

需要多步决策的技能，覆盖 `make_frame` 返回一个自己的 Frame（参考 `GanglieFrame`）。

**加一张牌**：继承 `CardSpec`，实现 `play_targets`（出牌阶段的合法目标）和 `effect`（对一个目标的结算），用 `@register_card` 注册，
然后在牌堆函数里加上它。锦囊的无懈可击询问、处理区清理都由通用的 `UseCardFrame` 完成。

**加一个牌堆**：`@register_deck("名字")` 注册一个返回 `[(牌名, 花色, 点数), ...]` 的函数，`Game(deck="名字")` 使用。

**加一个模式**：继承 `Mode`，实现 `setup`（座位、身份、武将）、`first_frame`（开局流程）、`on_death`（胜负判定）、
`is_enemy`（给 AI 用的敌我关系），用 `@register_mode` 注册。核心的距离、座次、濒死询问都已按 N 人写好，身份局只需要新写模式。

**加一个扩展包**：在 `sgs/packs/` 下新建子包并在 `sgs/packs/__init__.py` 里导入；第三方包也可以自己 import 后注册。

## 决策和动作空间

所有选择都表示为 `Decision(player, kind, prompt, options, ctx)`：

- `kind`：`play`（出牌阶段）、`respond`（要求出闪/杀/桃/无懈）、`trigger`（是否发动）、`choose`（其它单选）；
- `options[i].label` 是给人和 LLM 看的中文描述，`options[i].key` 是只含基本类型的元组，适合做动作编码；
- `ctx` 带上下文（是谁的杀、要哪种牌、判定原因等）。

为了让每一步的动作空间小，**多选都拆成逐张选择**（弃两张牌 = 选两次；制衡 = 逐张选，选“确定”结束）。
出牌阶段的“用哪张牌”和“指定谁”合并成一个选项（1v1 下组合很少）。

`Game(auto_single=True)`（默认）会自动跳过只有一个选项的决策，rollout 更快；写测试时用 `"responses"`，只自动跳过响应类决策。

## 隐藏信息

- `g.observe(seat)` 只给该玩家自己的手牌，对手只有手牌数；牌堆只有张数。`observe(None)` 是上帝视角。
- 日志每条有公开文本，私密部分（摸到什么牌）放在 `private[seat]` 里，`visible_logs(seat)` 按视角过滤。
- `g.determinize(seat, rng)`：复制一份并把该玩家看不到的牌（对手手牌 + 牌堆）随机重新分配，用于不偷看信息的 rollout。

## 确定性与性能

随机数生成器 `g.rng` 是状态的一部分，同种子 + 同动作序列一定得到同一局；拷贝后两边独立前进。

在一个普通云容器的单核上（`python -m sgs.tools.bench`）：

| 项目 | 数值 |
| --- | --- |
| 随机 vs 随机（关闭日志） | 约 240 局/秒，约 1.4 万决策/秒 |
| 规则 AI vs 规则 AI | 约 155 局/秒 |
| 中盘状态 `clone()` | 约 250 微秒 |
| 从中盘拷贝后随机打完 | 约 690 局/秒 |
| 规则 AI 对随机 AI 胜率 | 约 93%（双方先后手各测一半） |

纯 Python，多进程并行可以线性扩展。以后如果需要更快，可以把热点（选项生成、技能查询）缓存或用 Cython 重写，接口不变。
