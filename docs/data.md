# 导出数据格式（JSONL）

```bash
python -m sgs.export --games 1000 --agents rule rule -o data/duel.jsonl
python -m sgs.export --games 10000 --workers 8 -o data/big.jsonl.gz       # .gz 结尾自动压缩
python -m sgs.export --games 50 --rollouts 8 -o data/labeled.jsonl        # 附 rollout 估值（很慢）
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--agents rule random` | 两个座位用的 AI：`rule` / `random` / `llm?model=...` 等，写法见 [agents.md](agents.md) |
| `--generals 关羽 吕布` | 固定武将；不填就每局随机两名标准武将 |
| `--rollouts K` | 对每个合法动作做 K 次确定化 rollout，写入 `rollout_values` |
| `--include-auto` | 也导出只有一个选项、被自动选掉的决策（`auto: true`） |
| `--history full` | 每行放开局以来的全部可见日志；默认 `new` 只放上次决策以来的新日志 |
| `--no-privileged` | 不写上帝视角字段，文件小约 15% |
| `--workers N` | 多进程 |

同时会写一个 `<输出名>.cards.json`：牌 id 到牌（花色、点数、牌名）的对照表。同一个牌堆名下 id 固定，与种子无关。

## 每行字段

一行是一个玩家的一次决策。

| 字段 | 说明 | 能否给策略看 |
| --- | --- | --- |
| `format` | `sgs-decision/1` | |
| `game_id`、`seed`、`mode` | 对局标识；`seed` 加相同的动作序列可以完整复现这局 | |
| `step` | 这局第几个决策（含自动跳过的决策），从 0 开始 | 可以 |
| `turn`、`phase` | 回合数和阶段（`prepare/judge/draw/play/discard/finish`） | 可以 |
| `player`、`general`、`agent` | 决策者座位、武将、用的哪个 AI | 可以（`agent` 别给） |
| `agent_info` | 智能体附加信息（模型的：`source` 是 llm 还是兜底、`attempts`、`reason`、`errors`），只有模型等会给 | 不能 |
| `obs` | 决策者视角的观察，见下表 | 可以 |
| `history` | 决策者看得到的日志文本（含自己的私密信息，如摸到什么牌） | 可以 |
| `decision` | `kind`（play/respond/trigger/choose）、`prompt`、`ctx`（是谁的杀、要哪种牌、判定原因等） | 可以 |
| `legal_actions` | 合法动作列表，每个有 `key`（只含基本类型，可做动作编码）和 `label`（中文描述） | 可以 |
| `action`、`action_key`、`action_label` | 实际选了第几个动作 | 监督信号 |
| `rollout_values` | 每个合法动作的期望得分（胜 1、平 0.5、负 0），决策者视角，只用了决策者能看到的信息 | 监督信号 |
| `privileged` | **上帝视角**：`hands`（每人手牌 id）、`deck_order`（牌堆从顶到底的 id）、`discard`、`known`（已知手牌：牌 id → 知道它在谁手里的座位） | **不能**，只用于分析或训练 critic |
| `outcome` | 这局结果：`winner`、`result`、`turns`、`num_decisions`，以及 `reward`（决策者胜 1、负 -1、平 0） | 监督信号 |
| `auto` | 是否是自动选择的唯一选项（只在 `--include-auto` 时会出现 true） | |

`obs` 里有：

- `players`：每名角色的武将、势力、性别、体力和上限、存活、技能、手牌数、装备区、判定区（`as_name` 是当作的延时锦囊）、标记；
  决策者自己多一个 `hand`。**对手的手牌只有张数**，外加 `known_hand`：对手手里你确实知道的牌
  （每张带 `how` 来源、`turn` 第几回合、`from` 原来在谁那里，见下面“已知手牌”）；
  决策者自己多一个 `known_by`：`{"1": [牌…]}`，对手知道你手里的哪些牌；
- `deck_count`、`discard_count`、`discard_top`（弃牌堆最上面 10 张）、`processing`（处理区）；
- `slash_used`、`slash_limit`、`max_hand`：本回合已出杀次数、杀的次数上限、手牌上限；
- `distance`（`"0->1": 1`）和 `attack_range`。

## 已知手牌

人打牌不太记牌，但会记住“明面上”知道的牌：五谷丰登时对手拿走的、被顺手牵羊从你手里拿走的、你仁德交给对手的。
引擎给每张进了手牌的牌记下谁知道它在那里（`Game.known`），规则只有两条，所以是精确的、不用猜：

- **进入手牌时**：牌原来在装备区、判定区、处理区或弃牌堆（公开的地方，比如五谷丰登亮出的牌、奸雄拿的杀、洛神和天妒的判定牌、
  顺手牵羊拿走的装备、借刀杀人交出的武器），所有人都知道；原来在别人手里（顺手牵羊或突袭拿走的手牌、仁德或遗计交出的牌），
  拿到的人和原主知道；反间的牌会展示，所有人知道；从牌堆摸的只有自己知道。
- **离开手牌时删除**：用掉、打出、弃置、被拆、被拿走（拿走的话按上一条重新记）、阵亡时都会删，因为离开手牌时牌是亮出的
  （被拿走时双方都看到了）。

没有记牌统计（剩几张杀、几张闪），也不记观星看到的牌堆顶后来被谁摸走。`Game.determinize`（rollout 用）会把已知的牌留在原处，
只把不知道的牌重新分配。

## 视角隔离怎么保证

- 智能体拿到的是 `PlayerView`（`sgs/core/view.py`），它是决策点的快照，内部没有 `Game` 的引用；
  去查看不到的牌会抛 `HiddenCardError`。规则 AI 只用这个接口，测试里跑了 40 局确认没有偷看。
- `obs`、`history`、`legal_actions` 都从这个视角生成。测试检查了每一行 `obs` 里都不出现对手手牌的 id。
- 对手正在做什么决策（例如被问要不要出无懈可击）不会出现在你的观察里，免得从“被询问”推断出对手有某张牌。
- 已知手牌也按视角给：`known_hand` 里只有决策者确实知道的牌，测试覆盖了每种公开途径和离开手牌的情况，
  并在随机对局里检查每个视角的 `known_hand` 都是对手真实手牌的子集、离开手牌的牌不会留在里面。
- 只有 `privileged` 字段带上帝视角信息。训练策略时要把它删掉。

## 示例行（缩短了 obs 和 privileged）

```json
{"format": "sgs-decision/1", "game_id": "duel-0", "seed": 0, "mode": "duel", "step": 57, "turn": 15, "phase": "play",
 "player": 1, "general": "吕布", "agent": "rule",
 "obs": {"viewer": 1, "turn": 15, "current": 0, "phase": "play",
   "players": [
     {"seat": 0, "general": "关羽", "kingdom": "蜀", "hp": 3, "max_hp": 4, "skills": ["武圣"], "hand_count": 2,
      "equips": {"weapon": {"id": 53, "name": "诸葛连弩", "suit": "♣", "rank": "A"}, "armor": {"id": 55, "name": "八卦阵", "suit": "♣", "rank": "2"}, "...": "..."},
      "judges": []},
     {"seat": 1, "general": "吕布", "kingdom": "群", "hp": 4, "max_hp": 4, "skills": ["无双"], "hand_count": 4,
      "equips": {"plus_horse": {"id": 61, "name": "的卢", "suit": "♣", "rank": "5"}}, "judges": [],
      "hand": [{"id": 93, "name": "闪", "suit": "♦", "rank": "8"}, {"id": 32, "name": "桃", "suit": "♥", "rank": "4"},
               {"id": 40, "name": "桃", "suit": "♥", "rank": "8"}, {"id": 98, "name": "闪", "suit": "♦", "rank": "J"}]}],
   "deck_count": 69, "discard_count": 27, "processing": [{"id": 18, "name": "杀", "suit": "♠", "rank": "10"}],
   "slash_used": 0, "slash_limit": 1, "max_hand": 4, "distance": {"0->1": 1, "1->0": 2}, "attack_range": {"0": 1, "1": 1}},
 "history": ["吕布[1] 弃置了 ♣10杀、♣3杀", "吕布[1] 回合结束（手牌 4，体力 4/4）", "—— 第 15 回合：关羽[0] ——",
             "关羽[0] 摸了 2 张牌（摸牌阶段）", "关羽[0] 进入出牌阶段（手牌 5）", "关羽[0] 装备了 ♦K紫骍",
             "关羽[0] 使用了 ♦3顺手牵羊，目标 吕布[1]", "关羽[0] 获得了 吕布[1] 的♣2八卦阵", "关羽[0] 装备了 ♣2八卦阵",
             "关羽[0] 使用了 ♠10杀，目标 吕布[1]"],
 "decision": {"kind": "respond", "prompt": "关羽[0] 对你使用了杀，请使用一张闪",
              "ctx": {"source": 0, "slash": true, "ignore_armor": false, "mode": "use", "names": ["闪"]}},
 "legal_actions": [{"key": ["card", "闪", [93], null, []], "label": "使用 ♦8闪"},
                   {"key": ["card", "闪", [98], null, []], "label": "使用 ♦J闪"},
                   {"key": ["pass"], "label": "不出"}],
 "auto": false,
 "privileged": {"hands": {"0": [70, 66], "1": [93, 32, 40, 98]}, "deck_order": [7, 1, 11, 100, 0, 63, "..."], "discard": ["..."]},
 "action": 0, "action_key": ["card", "闪", [93], null, []], "action_label": "使用 ♦8闪",
 "outcome": {"winner": 1, "result": "吕布[1] 获胜", "turns": 38, "generals": ["关羽", "吕布"],
             "num_decisions": 157, "num_manual": 111, "reward": 1.0}}
```

## 大小和速度（单核）

- 规则 AI 对打，约 44 局/秒，平均每局约 95 行。
- 带 `privileged` 每行约 3 KB；`--no-privileged` 加 `.gz` 压缩后每局约 10 KB。
- `--rollouts 8` 会慢上百倍，适合小批量打标签或配合 `--workers`。
