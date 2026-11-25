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
cot: "off"        # 默认，不分析（YAML 里 off 要加引号，不加也行，会当成关闭）
cot: key          # 只在关键决策先分析
cot: all          # 每个决策都先分析
cot_max_chars: 150   # 分析最多写多少字（写在提示词里的要求）
max_tokens: 1024     # 开了思维链回复变长，留够
```

关键决策（`is_key_decision`）：是否发动技能、技能里的选择（观星、遗计、反馈拿哪张……）、过河拆桥和顺手牵羊选哪张、
弃牌、卖血候选（自己有遗计、反馈、奸雄、刚烈，被杀时有闪可出可不出）。出牌阶段出什么、普通的出不出闪、无懈可击不算。
想换规则：`cot_key: 你的模块:函数名`（参数 view, d，返回 True/False）。

需要分析的决策，题目末尾会要求“先写分析：……，最后一行只写 JSON：{"action": 编号}”。
编号只从最后的 JSON 取（没有 JSON 时看最后一行），不会把分析里“选 2 不好”的 2 当答案。
解析不出时和原来一样：告诉模型重答，`max_retries` 次后交给规则 AI 兜底，都计入解析失败率和兜底率。

分析文本记在每局 `llm_calls.json` 每条记录的 `analysis` 字段里（每次请求的 `attempts[].analysis` 也有），
录像里那一步会标“先分析”并显示分析，`game.md` 里是“模型的分析：……”。

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

四组 none / tips / cot / both，同一个模型、同一批种子，各打规则 AI，武将只从写了技巧的四个里抽
（`general_pool`，删掉这行就用全部武将）。结果在 `runs/compare-tips-cot-时间/comparison.md`：胜率和 95% 区间、
解析失败率、兜底率、决策数 / 先分析的决策数 / 请求数、平均和总 token。每组 10 局时区间大约 ±30%，
只看得出很大的差距；想下结论用 50 局以上。
