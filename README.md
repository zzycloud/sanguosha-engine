# sanguosha-engine：三国杀标准版规则引擎

一个纯 Python（无第三方依赖）的三国杀对局引擎，主要用来批量跑对局、给 LLM / RL 采集数据和做 rollout。

- **标准版规则**：108 张标准牌（含 EX，不含军争），标准包 25 名武将。
- **开闭原则**：卡牌、技能、武将、牌堆、模式都通过注册表插入，加新内容不用改核心。
- **状态可拷贝**：结算过程是显式的栈，任意决策点 `clone()` 约 250 微秒；同种子同动作序列结果完全一致。
- **视角隔离**：AI 只拿到自己的视角快照（`PlayerView`），读不到对手手牌和牌堆顺序；支持 determinize 做不偷看的 rollout。
- **完整记录**：每次询问都有结果，包括“没有出闪”“没有发动技能”和只有一个选项被自动选掉的决策。
- **训练数据导出**：批量对局写成 JSONL，每行一个决策点，含玩家视角观察、合法动作、选择、胜负和可选的 rollout 估值。
- **类 gym 接口**、随机 AI、规则 AI、录像、观察页面、终端/网页对战。
- **1v1 两人对决**模式先做好，模式是可替换的插件。

设计文档：[docs/design.md](docs/design.md)　规则与已知简化：[docs/rules.md](docs/rules.md)　导出数据格式：[docs/data.md](docs/data.md)

## 快速开始

需要 Python 3.9 以上，引擎本身没有第三方依赖。

```bash
git clone https://github.com/zzycloud/sanguosha-engine.git
cd sanguosha-engine
python3 -m venv .venv && source .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -e ".[dev]"                                  # 依赖只有 PyYAML，dev 额外装 pytest
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

**接你自己的模型来打**：所有设置写在一个配置文件里，`configs/` 下有带注释的示例
（规则 AI 对打、本地 vLLM、云 API、假模型、网页人机）。配置怎么读、模型文件和密钥放哪，先看 [configs/README.md](configs/README.md)；全部字段见 [docs/agents.md](docs/agents.md)，
整体设计见 [docs/harness.md](docs/harness.md)。

```bash
python -m sgs.arena configs/local-vllm.yaml                        # 改里面的 base_url 和 model
python -m sgs.arena configs/local-vllm.yaml --set match.games=100  # 命令行只做临时覆盖
python -m sgs.tools.serve configs/play-vs-model.yaml --human 0     # 网页里和模型打
# 没有模型也能先跑通：起一个本地假模型
python -m sgs.tools.fake_llm --port 8001 &
python -m sgs.arena configs/fake-model.yaml
# 同一个模型用新旧两套提示词各打规则 AI 100 局，比胜率、答错率、兜底率、token
python -m sgs.experiment configs/compare-prompts.yaml
```

提示词有两套：`default`（局面 + 最近 40 条日志）和 `v2`（局面 + 已知手牌 + 本回合和上一回合的关键事件），
配置里 `prompt: v2` 切换，区别见 docs/agents.md 的“两套提示词”。

提示词上还能加两样，各自单独开关，不开时提示词和原来一样（见 [docs/tips-cot.md](docs/tips-cot.md)）：
`tips: true` 把自己武将的实战技巧（`configs/tips/武将名.md`，你自己写）放进提示词；
`cot: key` 让模型在关键决策（发动技能、卖血、拆顺选牌、弃牌）先写三行短分析（默认共 80 字以内）再给编号，`cot: all` 每个决策都分析。
四组对比（无 / 技巧 / 分析 / 两者）：`python -m sgs.experiment configs/compare-tips-cot.yaml`。
导出成“三行短分析 + 编号”的训练数据：`python -m sgs_data.cot_export configs/data/cot-export.yaml`。

跑完打开输出目录里的 `index.html`：每局一行，点进去是这局的录像（每个决策点下面能展开模型的完整提示词和回复）
和 `game.md` 文字轨迹；每局的数据都在 `games/0001/` 这样的目录里，按步数对齐。连不上模型或报 HTTP 503 时，
先跑一次 `python -m sgs.arena 你的配置.yaml --check`，排查说明见 docs/agents.md。

**导出训练数据**（每行一个决策点，字段见 [docs/data.md](docs/data.md)）：

```bash
python -m sgs.export --games 1000 -o data/duel.jsonl
python -m sgs.export --games 10000 --workers 8 --no-privileged -o data/big.jsonl.gz
python -m sgs.export --games 50 --rollouts 8 -o data/labeled.jsonl     # 附每个动作的 rollout 估值
```

**造训练数据（关键局面 + 普通局面，往下模拟打标签，导出 SFT / DPO / 题库）**，
详见 [docs/data-gen.md](docs/data-gen.md)：

```bash
python -m sgs_data configs/data/demo.yaml          # 规则 AI 模拟，几分钟
python -m sgs_data configs/data/full.yaml          # 正式跑：强模型当裁判（密钥放环境变量）
python -m sgs_data.evalbank configs/data/evalbank.yaml --set bank=runs/data-demo-xxx/test.jsonl   # 用题库考模型
```

**跑测试和测速**：

```bash
python -m pytest                       # 295 个单测
python -m sgs.tools.bench --games 500  # 每秒对局数、clone 耗时、AI 胜率
python examples/rollout_label.py       # 在一个决策点给每个动作做 rollout 估值
```

## 用法：类 gym 接口

```python
from sgs.env import SgsEnv
from sgs.agents import RandomAgent, RuleAgent, decide

env = SgsEnv(seed=42, generals=["诸葛亮", "吕布"])   # 也可 pick="random" / pick="choose"
obs = env.reset()
agents = [RuleAgent(seed=0), RandomAgent(seed=1)]
while not env.done:
    seat = env.current_player()
    obs = env.observe(seat)                  # 只含该玩家能看到的信息（JSON 友好的 dict）
    action = decide(agents[seat], env.game)  # 只把决策者的视角交给 AI
    env.step(action)                         # 动作是 options 的下标
print(env.rewards())                         # 胜 +1 / 负 -1 / 平 0
```

自己写 AI：继承 `Agent`，实现 `choose(view, decision)`。`view` 是 `PlayerView`，
用 `view.hand`、`view.card(id)`、`view.players[i].hp`、`view.is_enemy(i)` 等看局面；看不到的牌会抛错。

```python
from sgs.agents import Agent

class MyAgent(Agent):
    def choose(self, view, d):
        for i, o in enumerate(d.options):      # o.key 是动作编码，o.label 是中文描述
            if o.label.startswith("使用") and "杀" in o.label:
                return i
        return len(d.options) - 1
```

注册一个名字就能在所有命令行里用（`--p0 mybot`、`--agents mybot rule`），也可以不注册，直接写 `模块:类名`：

```python
from sgs.agents import register_agent

@register_agent("mybot")
class MyAgent(Agent): ...
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

`python -m sgs.replay ... -o out.html` 生成一个自带数据的单文件 HTML，离线也能打开，逐步回放。
每个事件（摸牌、判定、弃牌、伤害……）和每次询问都是单独一步：

- 双方座位：武将、势力、体力珠、技能（标出触发技/主动技/锁定技等，悬停看原文；主公技灰色显示）、四个装备栏、判定区、手牌；
- 中间台面：回合数、阶段进度、处理区、弃牌堆顶、这一步的关键事件；
- 右侧：这一步的决策（谁、问什么、所有选项、选了哪个，唯一选项会标出“自动”）和按回合分组的日志，点日志可跳到那一步；
- 底部：双方体力走势图（点击跳转）、播放控制；方向键前进后退，空格播放；
- 视角切换：上帝视角，或只看某一方能看到的信息（对手手牌显示为背面，私密日志和对方被问了什么都跳过）。

也可以打开 `web/index.html` 再加载 `-o out.json` 生成的录像文件。

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
| 随机 vs 随机 | 约 270 局/秒（约 1.6 万决策/秒） |
| 规则 AI vs 规则 AI | 约 110 局/秒（每步都要生成视角快照） |
| 导出 JSONL（规则 AI 对打） | 约 44 局/秒 |
| 中盘 `clone()` | 约 300 微秒 |
| 规则 AI 对随机 AI 胜率 | 约 93% |

## 目录

```
sgs/core/            核心引擎（不认识任何具体武将）
sgs/packs/standard/  标准版：牌堆、卡牌、装备、技能、武将
sgs/modes/           游戏模式（duel = 1v1）
sgs/agents/          智能体接口和注册表、随机 AI、规则 AI、大模型（llm/）
sgs/arena.py         对战（读配置文件，接模型打、并发、出录像和数据）
sgs/config.py        配置文件的读取、默认值和覆盖
sgs/experiment.py    对比实验（同一设置换一个东西各打一轮，出对比表）
sgs/progress.py      对战时终端里的进度和心跳
configs/             带注释的示例配置
sgs/env.py           类 gym 接口
sgs/export.py        批量导出 JSONL
sgs/rollout.py       rollout 估值
sgs/replay.py        录像
sgs/tools/           测速、本地网页对战、本地假模型服务（fake_llm）
sgs_data/            造训练数据（关键局面模板、往下模拟打标签、裁判、配比导出、题库评测），只用引擎公开接口
web/index.html       观察页面
tests/               规则单测
docs/                设计文档、规则说明
```
