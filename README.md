# sanguosha-engine：三国杀标准版规则引擎

一个纯 Python（无第三方依赖）的三国杀对局引擎，主要用来批量跑对局、给 LLM / RL 采集数据和做 rollout。

- **标准版规则**：108 张标准牌（含 EX，不含军争），标准包 25 名武将。
- **开闭原则**：卡牌、技能、武将、牌堆、模式都通过注册表插入，加新内容不用改核心。
- **状态可拷贝**：结算过程是显式的栈，任意决策点 `clone()` 约 250 微秒；同种子同动作序列结果完全一致。
- **隐藏信息**：每个玩家的观察不含对手手牌和牌堆顺序；支持 determinize 做不偷看的 rollout。
- **类 gym 接口**、随机 AI、规则 AI、录像、观察页面、终端/网页对战。
- **1v1 两人对决**模式先做好，模式是可替换的插件。

设计文档：[docs/design.md](docs/design.md)　规则与已知简化：[docs/rules.md](docs/rules.md)

## 快速开始

需要 Python 3.9 以上，引擎本身没有第三方依赖。

```bash
git clone https://github.com/zzycloud/sanguosha-engine.git
cd sanguosha-engine
python3 -m venv .venv && source .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -e ".[dev]"                                  # 只额外装 pytest
```

**看一局 AI 对战的录像**（生成一个 HTML，用浏览器直接打开，方向键逐步回放）：

```bash
python -m sgs.replay --seed 1 --generals 关羽 吕布 -o replay.html
python -m sgs.replay --seed 7 --agents rule random -o replay2.html   # 不指定武将就随机
```

**自己和规则 AI 打**：

```bash
python -m sgs.cli --generals 诸葛亮 吕布          # 终端里打，输入选项编号，你是 0 号位
python -m sgs.tools.serve --generals 诸葛亮 吕布   # 网页里打，按提示打开 http://127.0.0.1:8000/?live=1&human=0
python -m sgs.tools.serve --human -1               # 网页里看两个规则 AI 实时对打
```

**跑测试和测速**：

```bash
python -m pytest                       # 108 个规则单测
python -m sgs.tools.bench --games 500  # 每秒对局数、clone 耗时、AI 胜率
python examples/rollout_label.py       # 在一个决策点给每个动作做 rollout 估值
```

## 用法：类 gym 接口

```python
from sgs.env import SgsEnv
from sgs.agents import RandomAgent, RuleAgent

env = SgsEnv(seed=42, generals=["诸葛亮", "吕布"])   # 也可 pick="random" / pick="choose"
obs = env.reset()
agents = [RuleAgent(seed=0), RandomAgent(seed=1)]
while not env.done:
    seat = env.current_player
    d = env.game.pending                     # 当前决策：d.prompt、d.options[i].label / .key
    obs = env.observe(seat)                  # 只含该玩家能看到的信息
    action = agents[seat].choose(env.game, d)
    env.step(action)                         # 动作是 options 的下标
print(env.rewards())                         # 胜 +1 / 负 -1 / 平 0
```

rollout：在任意决策点复制状态，换着动作往下打。

```python
g = env.game
for i, opt in enumerate(g.pending.options):
    world = g.determinize(seat, rng)   # 打乱自己看不到的牌，不偷看
    world.act(i)
    ...                                # 用任意 agent 打完，统计胜率
```

完整例子见 `examples/rollout_label.py`（对每个合法动作做多次 rollout，给出动作价值标签）。

## 观察页面

`python -m sgs.replay ... -o out.html` 生成一个自带数据的单文件 HTML，直接用浏览器打开就能逐步回放：
双方武将、体力、手牌（上帝视角）、装备、判定区、处理区、日志，以及每一步的决策选项和实际选择。
也可以打开 `web/index.html` 再加载 `-o out.json` 生成的录像文件。方向键前进后退，支持自动播放和调速。

## 扩展

都是新写一个模块、注册即可，详细说明在 [docs/design.md](docs/design.md#扩展开闭原则怎么落地)。

```python
from sgs.core.plugin import Skill
from sgs.core.registry import register_skill, register_general

@register_skill
class Biyue(Skill):
    name = "闭月"
    description = "结束阶段，你可以摸一张牌。"
    events = ("phase_finish",)
    def can_trigger(self, g, owner, event, ev):
        return ev.seat == owner
    def on_trigger(self, g, owner, event, ev):
        g.draw(owner, 1, "闭月")

register_general("貂蝉", "群", 3, "female", ("离间", "闭月"), pack="standard")
```

- 新武将 / 技能：`register_skill` + `register_general`，技能通过事件或规则钩子生效。
- 新卡牌：继承 `CardSpec`，`@register_card`，加进某个牌堆。
- 新牌堆：`@register_deck("名字")`。
- 新模式（如身份局、官方 1v1 赛制）：继承 `Mode`，`@register_mode`。
- 新扩展包（如风林火山）：在 `sgs/packs/` 下新建子包。

## 已实现内容

**牌（108 张）**：杀、闪、桃；决斗、过河拆桥、顺手牵羊、无中生有、借刀杀人、南蛮入侵、万箭齐发、桃园结义、五谷丰登、无懈可击；
乐不思蜀、闪电；诸葛连弩、青釭剑、雌雄双股剑、寒冰剑、青龙偃月刀、丈八蛇矛、贯石斧、方天画戟、麒麟弓；八卦阵、仁王盾；六匹坐骑。

**武将（25 名）**：曹操、司马懿、夏侯惇、张辽、许褚、郭嘉、甄姬、刘备、关羽、张飞、诸葛亮、赵云、马超、黄月英、
孙权、甘宁、吕蒙、黄盖、周瑜、大乔、陆逊、孙尚香、华佗、吕布、貂蝉（主公技除外）。

没做和简化的部分列在 [docs/rules.md](docs/rules.md#已知的简化和没做的部分)。

## 性能（单核，纯 Python）

| 项目 | 数值 |
| --- | --- |
| 随机 vs 随机 | 约 240 局/秒（约 1.4 万决策/秒） |
| 规则 AI vs 规则 AI | 约 155 局/秒 |
| 中盘 `clone()` | 约 250 微秒 |
| 规则 AI 对随机 AI 胜率 | 约 93% |

## 目录

```
sgs/core/            核心引擎（不认识任何具体武将）
sgs/packs/standard/  标准版：牌堆、卡牌、装备、技能、武将
sgs/modes/           游戏模式（duel = 1v1）
sgs/agents/          随机 AI、规则 AI
sgs/env.py           类 gym 接口
sgs/replay.py        录像
sgs/tools/           测速、本地网页对战
web/index.html       观察页面
tests/               规则单测
docs/                设计文档、规则说明
```
