# 造训练数据（sgs_data）

`sgs_data/` 是独立的一个包，只用引擎的公开接口（`Game`、`clone`、`determinize`、智能体注册表、提示词），
引擎本身没改。思路的完整说明见数据方案文档，这里只写怎么用。

## 一句话

两类题：**普通局面**（正常打一局随机抽决策点，保基本功，默认占 70%）和**关键局面**（按模板直接摆出要想的局面，
带随机变化和反例，默认占 30%）。每道题对每组选项在同一批“随机世界”上往下打到终局比胜率，
只留“有选项明显更差”的题。可选：强模型当裁判，和模拟结论不一致的题不进训练、写进复核表。

## 怎么跑

```bash
python -m sgs_data configs/data/demo.yaml          # 规则 AI 模拟，几分钟，不调模型
export MY_API_KEY=sk-...                           # full.yaml 用强模型当裁判
python -m sgs_data configs/data/full.yaml
python -m sgs_data configs/data/demo.yaml --set key.scenarios.guojia_sell_hp=200   # 临时改一项
python -m sgs_data.evalbank configs/data/evalbank.yaml --set bank=runs/data-demo-xxx/test.jsonl
```

终端每 2 秒一行进度（做了多少、多快、还要多久、多少题有结论）。

## 输出（runs/data-名字-时间/）

| 文件 | 内容 |
| --- | --- |
| `sft.jsonl` | `{"messages": [system, user, assistant {"action": k}], "meta": {...}}` |
| `dpo.jsonl` | `{"prompt": [...], "chosen": [...], "rejected": [...], "margin": 胜率差, "meta": {...}}` |
| `test.jsonl` | 题库（种子和训练题分开，模拟局数翻倍），给 `sgs_data.evalbank` 用 |
| `review.md` | 给人看：上帝视角的局面、每组选项胜率 ± 误差、裁判的票和理由 |
| `stats.md` | 每类题造了多少、留了多少、答案分布 |
| `items/*.jsonl` | 每道题的全部信息（配方、分组、每组胜率、标准误、状态）；`failed.jsonl` 是出错的题 |
| `config.yaml` | 这次实际用的配置（密钥已隐去） |

`meta` 里有 `scenario`、`general`、`tag`（答案的类别，如“出闪”/“不出”）、`values`（每组胜率），
方便按武将/模板切数据、按 margin 加权。

## 配置项（见 configs/data/*.yaml 里的注释）

- `rollout.policy`：谁来往下打。`rule+`（默认，规则 AI + 观星会避闪电）、`rule`，或者一个模型
  （和对战配置的 players 写法一样，可以 `include`）。模型当模拟者时改用线程并发，建议配 `max_turns_ahead`。
- `rollout.n_min / n_max / margin / z`：每组至少/最多模拟几个世界；胜率差小于 margin 算差不多；
  差值减 z 倍标准误仍大于 0 才算明显更差。
- `key.scenarios`：模板名: 题数（或 `{count, test, params}`）。`key.test_per_scenario`：每个模板的题库题数。
- `normal.games / per_game / players / general_pool / focus / skip_kinds`。
- `judge.enabled / player / votes / on / on_disagree`（`review` 进复核表 / `drop` 扔掉 / `keep` 照样用）。
- `mix.key_share / max_tag_share / max_general_share / dpo_pairs_per_item`。

## 现有模板

| 名字 | 武将 | 问什么 |
| --- | --- | --- |
| `guojia_sell_hp` | 郭嘉 | 被杀时出闪还是扣血发遗计 |
| `guojia_yiji_kongcheng` | 郭嘉 | 遗计的牌自己留还是塞给空城诸葛亮 |
| `zhuge_kongcheng` | 诸葛亮 | 出牌阶段打光手牌凑空城还是留牌 |
| `zhuge_lightning` | 诸葛亮 | 要不要放闪电（对面 40% 是司马懿） |
| `simayi_fankui` | 司马懿 | 反馈拿连弩、别的装备还是手牌 |
| `zhenji_discard` | 甄姬 | 超上限弃什么 |

加模板：在 `sgs_data/scenarios/` 新建一个文件，继承 `Scenario`、加 `@register_scenario`，
`build(seed)` 里用 `Board` 摆局面（清手牌、发指定的牌、设血量、上装备、`finish()` 随机化回合数和牌堆，
再用 `until(...)` 推进到要决策的那一刻），返回 `(game, variant)`；`tag(desc)` 把选项描述归成几类答案。
同一个种子必须摆出完全一样的局面（题目只存“模板 + 种子”）。

## 和引擎的关系

- 观星：引擎的 `Game.determinize` 不保留观星看到的牌堆顶/底。`sgs_data.label.determinize` 在它之后补上
  （观星后还在原位的牌不动）。1v1 观星只看 2 张，放顶上的多半在摸牌阶段就被摸走，影响主要在判定阶段和放到底下的牌。
- `rule+`（`sgs_data/policy.py`）：在规则 AI 上只改观星——自己判定区有闪电时把不劈人的牌放顶上。
- 普通局面不抽“观星安排到一半”的决策点（那几张牌被看过但还在牌堆里，随机重发会弄乱）。
