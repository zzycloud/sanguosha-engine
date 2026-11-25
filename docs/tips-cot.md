# 武将技巧和思维链

两个提示词选项，写在模型那一方的配置里，各自单独开关。都不开时提示词和行为跟原来完全一样。
代码在 `sgs/agents/llm/addons.py`，包在任何提示词模板（default、v2、你自己的）外面，引擎没改。

## 武将技巧（tips）

`configs/tips/武将名.md`，一个武将一个文件，内容随便写，几条短句最好。选到谁就注入谁的文件。
郭嘉、诸葛亮、司马懿、甄姬四个是按你之前说的心得写的草稿，直接改。`<!-- 注释 -->` 不会进提示词。

```yaml
tips: true                                       # 只注入自己武将的
tips: {dir: configs/tips, self: true, opponent: true}   # 对手武将的也注入（默认不注入）
```

注入到 system 消息里，格式说明前面，标题会自动写明“玩家经验，只是参考，要结合当前局面判断，和局面冲突时以局面为准”。
没有文件的武将什么都不加。目录是相对路径时，先按当前目录找，再按仓库根目录找。

## 思维链（cot）

```yaml
cot: "off"            # 默认，不分析（YAML 里 off 要加引号，不加也行，会当成关闭）
cot: key              # 只在关键决策先分析
cot: all              # 每个决策都先分析
cot_source: content   # 分析从哪来，见下面
cot_max_chars: 80     # 三行分析加起来最多多少字（不算空白，含“局面：”这些标签）
cot_max_tokens: null  # 需要分析的请求单独的 max_tokens；null = 用 max_tokens。普通模型可写 auto（= 字数 × 2 + 60）
cot_retries: 1        # 分析的回复解析不出编号（多半是被截断）时重答几次，再不行交给规则 AI
max_tokens: 4096      # 推理模型（思考可能也算 token）设宽，别用它卡分析长度
reasoning_effort: low       # 推理模型的思考强度，原样发给服务端；不写就不发
cot_reasoning_effort: high  # 只在关键决策用的思考强度（不写就和 reasoning_effort 一样）
```

关键决策（`is_key_decision`）：是否发动技能、技能里的选择（观星、遗计、反馈拿哪张……）、过河拆桥和顺手牵羊选哪张、
弃牌、卖血候选（自己有遗计、反馈、奸雄、刚烈，被杀时有闪可出可不出）。出牌阶段出什么、普通的出不出闪、无懈可击不算。
想换规则：`cot_key: 你的模块:函数名`（参数 view, d，返回 True/False）。

### 分析从哪来：cot_source

- `content`（默认）：题目末尾要求模型先写三行，再写编号：

  ```
  局面：对面 1 血没有手牌，我有杀和过河拆桥
  得失：直接杀他没闪可出；先拆没有必要
  结论：直接杀
  {"action": 0}
  ```

  编号只从最后的 JSON 取（没有 JSON 时看最后一行）。推理模型会先在思考字段里想，再在回答里写这三行，
  三行就是它自己对思考的总结，和它选的编号是同一次回答出来的。
- `reasoning`：提示词不变，只要编号。推理模型的思考字段原文记下来（`reasoning` 和 `reasoning_content` 两种字段名都认，
  例如 step-5-preview 默认用 `reasoning`），导出训练数据时再用压缩模型改成三行。好处是用的是它真正的思考，
  代价是导出时每条要多一次压缩请求，压缩的人不是做决定的人，可能总结得不准（压缩提示词里会给它最后的选择）。

两种都会把思考字段原文和服务端原始返回存在 `llm_calls.json` 的 `attempts[].reasoning` 和 `attempts[].response` 里。
思考字段原文不会直接进训练数据。

### 长度怎么控制

1. 提示词要求固定三行、总字数不超过 `cot_max_chars`，附一个短示例。
2. 事后数字数：编号拿到了但分析超过上限，记“分析超长”，这一步照常用；回复写到 `max_tokens` 被截断、没写到编号，
   记“截断”，重答 `cot_retries` 次，再不行交给规则 AI。两个数都在 comparison.md 的“分析超长 / 截断”一列。
3. 导出训练数据时按字数过滤（下面）。

分析文本记在 `llm_calls.json` 每条记录的 `analysis` 字段里（`cot_source: reasoning` 时是思考原文），
录像里那一步会标“先分析”并显示分析，`game.md` 里是“模型的分析：……”。

## 导出训练数据

```bash
uv run python -m sgs_data.cot_export configs/data/cot-export.yaml
uv run python -m sgs_data.cot_export configs/data/cot-export.yaml --set overlong=compress
```

读 `runs` 下面所有 `games/*/llm_calls.json`，只要模型自己答出来的决策（兜底的不要），写成
“三行分析 + 编号”的 SFT 数据。提示词统一换成三行格式（`cot_source: reasoning` 跑出来的也换）。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `runs` | — | 对局输出目录，可以写通配符 |
| `agents` | `[]` | 只要这些显示名的决策，空 = 全要 |
| `only_wins` | `false` | 只要赢了那一方的决策 |
| `analysis_from` | `auto` | `auto` 按对局时的 cot_source；`content` / `reasoning` 强制指定 |
| `max_chars` | `80` | 三行加起来最多多少字 |
| `overlong` | `drop` | content 分析超长或不是三行：`drop` 丢掉 / `compress` 让压缩模型改 / `keep` 照样留 |
| `require_three_lines` | `true` | 不是标准三行也算不合格 |
| `compressor` | — | 压缩用的模型（写法和 players.A 一样）；`compress` 或分析来自 reasoning 时必须写 |
| `compress_retries` | `1` | 压缩结果超长或不是三行时再压几次，还不行丢掉 |
| `concurrency` | `4` | 压缩请求并发数 |
| `include_plain` | `false` | 非关键决策也导出（答案只有编号） |
| `drop_tips` | `false` | 把对局时注入的武将技巧从提示词里删掉 |
| `cot_mode` | `key` | 导出提示词的格式说明，和你推理时的 cot 一样 |

输出：`sft.jsonl`、`rows.jsonl`（每条的来源、原分析、压缩结果）、`stats.md`（多少条合格、丢掉、压缩）、`config.yaml`。

## 对比实验

```bash
# 不花钱先试：另一个终端起假模型
uv run python -m sgs.tools.fake_llm --port 8001
uv run python -m sgs.experiment configs/compare-tips-cot-fake.yaml

# 正式：先改 configs/models/strong-api.yaml 的 base_url 和 model
export MY_API_KEY=你的密钥
uv run python -m sgs.experiment configs/compare-tips-cot.yaml                          # 每组 10 局
uv run python -m sgs.experiment configs/compare-tips-cot.yaml --set base.match.games=50   # 每组 50 局
```

四组 none / tips / cot / both（想比较两种分析来源，把配置里注释掉的 both_reason 打开），同一个模型、同一批种子，各打规则 AI，武将只从写了技巧的四个里抽
（`general_pool`，删掉这行就用全部武将）。结果在 `runs/compare-tips-cot-时间/comparison.md`：胜率和 95% 区间、
解析失败率、兜底率、决策数 / 先分析的决策数 / 请求数、分析超长 / 截断、平均和总 token。每组 10 局时区间大约 ±30%，
只看得出很大的差距；想下结论用 50 局以上。
