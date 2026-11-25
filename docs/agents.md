# 接模型来打：智能体框架

所有“玩家”——规则 AI、随机 AI、你部署的模型、商用 API、你自己的策略服务——都实现同一个接口，
引擎只认这个接口。接模型不需要改引擎，换模型只是换一行配置。

```
引擎 ── decide(agent, g) ──> agent.choose(view, decision) ──> 选项下标
         只交出决策者的视角       view：PlayerView（看不到对手手牌和牌堆）
                                 decision：kind / prompt / options[i].label / options[i].key
```

- `view` 是快照，不持有 Game，智能体没有办法偷看；
- `decision.options` 里的每个动作都已经过规则检查，智能体只需要挑一个编号；返回越界的编号会直接报错；
- 只有一个选项的决策（例如被迫弃最后一张牌）由对战循环直接选掉，不会调模型。

整体分层和数据流见 [harness.md](harness.md)。

## 用配置文件指定一切

对战的所有设置写在一个 YAML 文件里（也支持 JSON、TOML），运行时只给文件路径：

```bash
python -m sgs.arena configs/local-vllm.yaml
python -m sgs.arena configs/local-vllm.yaml --set match.games=100 --set players.A.temperature=0   # 临时覆盖
python -m sgs.arena configs/local-vllm.yaml --check     # 只检查配置、打印合并默认值后的结果，不开打
```

仓库里的示例（都有逐行注释）：

| 文件 | 内容 |
| --- | --- |
| `configs/rule-vs-random.yaml` | 最简配置：规则 AI 对随机 AI |
| `configs/local-vllm.yaml` | 自己部署的模型对规则 AI，**列出了全部字段**，当字段手册用 |
| `configs/cloud-api.yaml` | 云 API 对本地模型，演示密钥放环境变量、`include` 共用模型配置 |
| `configs/fake-model.yaml` | 接本地假模型，不花钱先跑通流程 |
| `configs/play-vs-model.yaml` | 网页里人和模型打：`python -m sgs.tools.serve configs/play-vs-model.yaml --human 0` |
| `configs/models/qwen-local.yaml` | 单独一个模型的配置，被上面的文件 `include` |

配置分三段：

```yaml
match:          # 怎么打：局数、种子、换边、武将、并发、超时
  games: 20
  swap: true
  generals: random          # 或 [0 号位武将, 1 号位武将]
  concurrency: 8
players:        # 谁来打：A、B 各一段，agent 是类型，其余字段交给这个智能体
  A:
    agent: llm
    base_url: http://localhost:8000/v1
    model: Qwen/Qwen2.5-7B-Instruct
    temperature: 0.7
  B:
    agent: rule
output:         # 输出什么：目录、录像局数、决策数据、模型调用记录、rollout
  dir: runs/{name}-{time}
  replays: 5
```

- 没写的字段用默认值（默认值见 `sgs/config.py` 的 `DEFAULTS`）；写错字段名会直接报错并列出可用字段；
- `players.X` 也可以只写一个字符串，例如 `B: rule`；
- `include: models/xxx.yaml` 先读另一个文件（相对当前配置文件），本段里写的字段再覆盖它；
- 字符串里的 `${环境变量}` 会被展开，密钥建议这样传或用 `api_key_env`；
- 输出目录里会存一份 `config.yaml`：这次实际用的完整配置（密钥打码），方便复现。

`agent` 可以是：`rule`、`random`、`llm`，你用 `@register_agent("名字")` 注册的名字，或 `包.模块:类名`。

## 跑的时候终端里看到什么

所有进度都打到 stderr，默认 `output.progress: normal`：

```
对战开始：20 局，同时 8 局，换边（每两局同一副牌交换座位），单局时限 3600 秒，输出到 runs/8b-vs-8b-20261125-165000
  A：llm:your-model（http://127.0.0.1:8000/v1/chat/completions，不走代理，单次请求超时 60.0 秒，重试 3 次，一个决策最多 300 秒）
  B：llm:your-model（同上）
连通性检查 A：通了，回复用了 0.4 秒
[开始] 第 1/20 局：张飞（0 号位，llm:your-model），黄盖（1 号位，llm:your-model）
[进度 16:50:30，已用 30 秒] 完成 0/20（A 0 : 0 B）
    第 1 局 张飞/黄盖 第 3 回合，25 个决策，模型 18 次 平均 1.2 秒，正在等 张飞 的模型回复
[请求失败] 第 3 局 黄盖（llm:your-model）：HTTP 503，响应体为空，1.0 秒后重试（第 2/4 次）
[兜底] 第 3 局 第 5 回合 黄盖：模型没给出合法动作，用兜底 AI 选了“打出 ♣6杀”；最后的错误：…
[结束] 第 1/20 局 张飞/黄盖：张飞[0] 获胜，胜方 A，15 回合，212.4 秒；模型决策 70（请求 72 次），答错 2，兜底 0。总比分 A 1 : 0 B，完成 1/20，已用 3 分 32 秒
```

- 开打前先对每个模型方发一次最小请求（`output.connect_check`，默认开），不通就打印诊断、退出码 1，不开打；
- `[进度]` 是心跳，每 `output.heartbeat` 秒（默认 30）一行：进行中的每局打到第几回合、做了多少决策、
  模型平均用时、答错/兜底/请求失败次数、正在等谁的回复、等了多久（超过 5 秒才显示）。
  心跳一直在走、决策数在涨就是在跑；同一个“已等”越来越大说明卡在模型那边；
- 请求失败、兜底、单局超时（`[超时]`）发生时立即一行；
- `output.progress: quiet` 只打开头的配置摘要、出错和最后的汇总；`verbose` 再加每个模型决策一行。

**超时默认值**：单次请求 `timeout` 60 秒（失败按 `http_retries` 重试），一个决策点 `decision_timeout`
300 秒（含所有重试，超了交给兜底），一局 `match.game_timeout` 3600 秒（超了中止这局，已打的部分保留）。
都可以改，写 `null` 表示不限（不建议，模型卡住时会一直等）。

**中途按 Ctrl+C**：不再开新局，正在打的局在当前请求返回后中止，已经写下的文件全部保留，
结果记为“手动中止（Ctrl+C）”，`index.html`、`summary.json` 照常生成。再按一次立即退出（退出码 130）。

## 对战的输出

打开输出目录里的 **`index.html`**：每局一行（武将、胜负、模型决策和请求次数、答错、兜底、请求失败），
可以只看出问题的局、点表头排序；每行有“录像”和“轨迹”链接。每局开始和结束都会更新，正在打的局显示“进行中”。

文件是边打边写的：一局开始就建 `games/0001/`，`result.json` 先写一个“进行中”的占位；`game.md`
每个决策点追加一节；`trajectory.json`、`llm_calls.json` 最多每 2 秒整份重写一次（先写临时文件再改名，
任何时候打开都是完整的 JSON）；`replay.html` 在一局结束（或中止）时写。

```
runs/xxx/
  index.html            总索引
  summary.json          A、B 各自胜场、先手胜场；模型方另有决策次数、请求次数、答错和兜底的次数与比例、
                        按类型统计的请求失败（error_kinds，例如 {"HTTP 503": 44}）、平均耗时、token 用量
  games.json            每局一条汇总（和索引页同样的信息）
  config.yaml           实际使用的完整配置（合并默认值和覆盖，密钥打码）
  cards.json            牌 id 对照表：标准牌堆每张牌的 id、牌名、花色、点数（轨迹里上帝视角字段
                        hands / deck_order 等只写牌的 id，用它查是哪张牌）；开打前就写好
  games/0001/           每局一个目录，里面的东西用同一套编号对齐：
    replay.html         录像。每个决策点下面列出模型的每次请求，点开看完整提示词、思考、原始回复、
                        解析结果，请求失败时还有 HTTP 诊断；底部有“上个/下个模型决策”“上个/下个出问题的”按钮
    game.md             整局按时间顺序的文字轨迹：两次决策之间发生了什么（上帝视角），每个决策点的问题、
                        全部选项、选了哪个、来源（模型 / 兜底）、答错原因、模型理由，以及提示词和回复原文
    trajectory.json     每个决策点一项：决策者视角的观察 obs、看到的日志 history、合法动作、选择、
                        agent_info、最终胜负 outcome（格式同 sgs.export 的一行，见 docs/data.md）
    llm_calls.json      这局每次交给模型的决策一项（格式见下）
    result.json         这局汇总，出错时含完整 traceback
  decisions.jsonl       可选（output.decisions: true）：所有局的决策点合并成一个 JSONL，训练用
  llm_calls.jsonl       可选（output.llm_calls: true）：所有局的模型调用合并成一个 JSONL，训练用
```

对齐用的编号：

| 编号 | 在哪 | 意思 |
| --- | --- | --- |
| `replay_step` | trajectory.json、llm_calls.json、game.md 的小标题 | 录像第几步。录像右下角显示“第 N 步”，地址后面加 `#step=N` 直接跳过去 |
| `step` / `decision_step` | trajectory.json / llm_calls.json | 引擎里这局第几个决策（含只有一个选项、自动选掉的） |
| `llm_calls` / `call` | trajectory.json 每项 / llm_calls.json 每项 | 这个决策对应 llm_calls.json 里的哪几项（下标） |

录像页面每局约 0.3～1 MB；跑几百上千局造数据时，把 `output.replays` 改成一个小数字或 `none`，
或者把 `output.per_game` 关掉、只开合并 JSONL。

换边（`swap: true`）是复式赛制：每两局用同一个种子，牌堆和两个座位的武将完全一样，只是 A、B 交换座位。
1v1 先手优势明显，比较两个模型时建议开着。

Python 里也可以直接调：

```python
from sgs import config
from sgs.arena import run_config
cfg = config.load("configs/local-vllm.yaml", ["match.games=4"])
summary = run_config(cfg)
```

## 接你自己部署的模型

先用推理框架起一个 OpenAI 兼容服务，再把地址和模型名写进配置（`configs/local-vllm.yaml` 改两行即可）：

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8000                                   # vLLM
python -m sglang.launch_server --model-path Qwen/Qwen2.5-7B-Instruct --port 30000  # SGLang：base_url 用 :30000/v1
ollama run qwen2.5:7b                                                               # Ollama：base_url 用 :11434/v1
# 你自己微调的 LoRA：vLLM 加 --enable-lora --lora-modules sgs=/path/to/lora，配置里 model: sgs
```

## 接商用 API

任何提供 OpenAI 兼容 `/chat/completions` 的服务都能直接用，见 `configs/cloud-api.yaml`。
**密钥只放环境变量**（`api_key_env: MY_API_KEY` 或 `api_key: ${MY_API_KEY}`），不会写进任何输出文件。
付费 API 按 token 收费：一个决策点的提示词约 1000～2000 字，一局 30～80 个决策点，先跑 2 局看统计再放大。

## 智能体配置项（players.X 下面）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `agent` | | `llm`（下面的字段都是它的） |
| `client` | `openai` | 协议：`openai` / `vllm` / `sglang` / `ollama` 或 `模块:类名` |
| `base_url` | 各 client 的默认地址 | 到 `/v1` 为止 |
| `model` | | 发给服务端的模型名 |
| `api_key` / `api_key_env` | 无 / `OPENAI_API_KEY` | 直接给密钥，或从哪个环境变量读；本地服务通常不需要 |
| `headers` / `extra_body` | | 额外请求头 / 合并进请求体的字段（如 `top_k`、关闭思考的开关） |
| `timeout` | 60 | 单次请求最多等几秒 |
| `http_retries` / `backoff` | 3 / 1.0 | 429、5xx、超时、连不上时重试几次，指数退避的基数（秒） |
| `max_retry_after` | 60 | 服务端回了 `Retry-After` 就按它等，但最多等这么久（秒） |
| `proxy` | `auto` | `auto`：本机和内网地址不走代理，其它按系统代理设置；`none`：不走代理；`system`：完全按系统设置；或写代理地址 |
| `temperature` / `top_p` / `max_tokens` / `stop` / `sample_seed` | 0.7 / - / - / - / - | 采样参数，没填的不发 |
| `prompt` | `default` | 提示词模板：`default`（旧）、`v2`（新，见下面“两套提示词”），或 `模块:类名` |
| `history_lines` | 40 | `default` 模板带最近多少条可见日志（0 不带） |
| `prompt_args` | | 交给模板的其它参数，例如 v2 的 `{recent_turns: 2, keep_flow: false, max_events: 60}` |
| `ask_reason` | false | 要求模型输出 `{"reason": ..., "action": ...}`，理由写进数据 |
| `system_prompt` / `extra_rules` | | 替换系统提示 / 在系统提示后追加文字（例如策略提示） |
| `show_skill_text` | true | 附上场上技能和装备的原文 |
| `max_retries` | 2 | 输出不合法时，把错误告诉模型再问几次 |
| `decision_timeout` | 300 | 一个决策点总共最多花几秒（含重试），超过就兜底 |
| `fallback` | `rule` | 兜底智能体；`none` 表示不兜底、直接中止这局 |
| `skip_single` | true | 只有一个选项、或剩下的牌都得选（顺序无关）时不调模型 |
| `label` | `llm:模型名` | 统计和数据里显示的名字 |

## 两套提示词

| | `default`（旧） | `v2`（新） |
| --- | --- | --- |
| 局面 | 双方武将、体力、技能原文、装备、判定区、距离、你的手牌、对手手牌数 | 同左，另外：对手手牌里**你确实知道的牌**和来源（“手牌 3 张，其中你知道的：♠7杀（第 1 回合五谷丰登，亮出过）”），以及**对手知道你手里的哪些牌** |
| 近况 | 最近 40 条可见日志，含“进入出牌阶段”“结束出牌阶段”“回合结束”这类流程记录，往往只够一两个回合 | 只放本回合和上一回合，按回合分组，去掉流程记录，保留出牌、响应和没响应、伤害、回复、获得/失去牌、技能、判定 |
| 装备说明 | 视角里看得到的所有装备，包括弃牌堆里的 | 只说明场上、你手里和对手已知手牌里的装备 |

已知手牌怎么算见 [data.md 的“已知手牌”](data.md#已知手牌)：只记明面上知道的，不做记牌统计。
同一批规则 AI 对局里平均每个提示词 v2 比 default 短约 25%，其中近况部分短约 60%。
v2 的近况可以调回旧方式：`prompt_args: {recent_turns: 0}` 就是最近 `history_lines` 条日志；`keep_flow: true` 保留流程记录。

**不需要想的决策不调模型**：只有一个合法动作，或者剩下的牌都得选（例如刚烈要弃两张而手里正好两张），
对战时直接选掉，不交给智能体，记在 `trajectory.json` 的 `auto_decisions` 里（`auto` 写原因），game.md 里标“（自动）”。

### 新旧提示词对比

```bash
python -m sgs.experiment configs/compare-prompts.yaml                          # 默认各打规则 AI 100 局
python -m sgs.experiment configs/compare-prompts.yaml --set base.match.games=10  # 先少打几局试试
```

`configs/compare-prompts.yaml` 里 `base` 是一份普通对战配置（改成你的模型），`variants` 下每个变体在它上面覆盖
（这里只换 `prompt`）。两个变体用同样的种子，第 k 局双方武将和牌堆完全一样。每个变体一个子目录（和普通对战一样有 index.html），
总目录里 `comparison.md` / `comparison.json` 是对比表：

| 变体 | 局数 | 胜率（95% 区间） | 先手胜 | 解析失败率 | 兜底率 | 请求失败 | 决策 / 先分析 / 请求 | 平均提示 token | 平均回复 token | 总 token | 平均用时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

解析失败率 = 没解析出合法编号的请求 / 请求次数（以前叫答错率），兜底率 = 兜底次数 / 模型决策次数，
“先分析”是开了思维链的决策数，平均 token 是每次请求的平均值（服务端返回的 usage），总 token 是整组加起来。
武将技巧和思维链的对比实验见 [tips-cot.md](tips-cot.md)。
100 局的胜率 95% 区间大约 ±10%，两组差距比这小时说明不了谁更好，可以加局数。

## 一次决策发生了什么

1. **渲染提示词**（`sgs/agents/llm/prompt.py`）：system 里是规则说明和输出格式；user 里是局面
   （你和对手的武将、体力、技能、你的手牌、对手手牌数、装备、判定区、距离、攻击范围、本回合出杀次数、
   技能和装备原文）、最近发生的事、要做的决定和编号后的全部合法动作。
2. **调模型**，单次请求超时、网络错误和 429/5xx 由客户端重试。
3. **解析**（`parse.py`）：先去掉 `<think>…</think>`，再找 JSON 的 `action`，再找“选择：N”，再看整句是不是一个数字
   或正好是某个选项的文字。
4. **不合法就重问**：把模型的回答和“你的回答不合法：编号 7 超出范围，请从 0 到 4 中选……”追加进对话再问，
   最多 `max_retries` 次。
5. **兜底**：请求失败、超时或一直不合法，交给兜底智能体（默认规则 AI），对局不会卡住；`summary.json` 里能看到兜底比例。
6. **记录**：整个过程记一条模型调用，写进这局的 `llm_calls.json`（开了 `output.llm_calls` 时也写进合并的 JSONL）。

每条模型调用（`sgs-llm-call/1`）：

| 字段 | 说明 |
| --- | --- |
| `game_id` / `seat` / `step` / `turn` / `phase` | 哪局、哪个座位、第几个决策 |
| `template` | 用的提示词模板（`default` / `v2`） |
| `call` / `replay_step` / `decision_step` | 这局第几次模型调用、录像第几步、引擎第几个决策（对齐用） |
| `concurrency` | 跑这局时同时进行的对局数 |
| `agent` / `model` | |
| `kind` / `prompt` / `legal_actions` | 决策类型、提示、全部合法动作文字 |
| `attempts` | 每次请求一项：`messages`（完整对话）、`request`（采样参数，不含密钥）、`response`（服务端原始返回）、`text`、`reasoning`（服务端单独给的思考内容）、`latency`、`usage`、`parsed`、`error`、`error_kind`（`illegal` 表示输出不合法，`HTTP 503`、`timeout`、`connect` 等表示请求失败），请求失败或中间重试过时还有 `http`（见下面“请求失败排查”） |
| `action` / `action_label` | 最终动作 |
| `source` | `llm`：模型给的；`fallback`：兜底给的 |
| `reason` | 模型给的理由（`ask_reason=true` 时） |

做 SFT：取 `source == "llm"` 的记录，用最后一次 attempt 的 `messages` 和 `text`；
配合 `decisions.jsonl` 里同一 `game_id` + `step` 的 `outcome.reward` 或 `rollout_values` 可以筛选或做偏好对。

## 不花钱、不用 GPU 先跑通

`sgs/tools/fake_llm.py` 是一个本地假模型服务（OpenAI 兼容），按 `model` 名模拟各种情况：

| model | 行为 |
| --- | --- |
| `fake` | 随机选一个合法编号，回 JSON |
| `fake-think` | 先输出 `<think>…</think>` 再给 JSON |
| `fake-flaky` | 第一次乱答，被要求重答后给合法编号 |
| `fake-bad` / `fake-range` | 永远乱答 / 永远越界（测兜底） |
| `fake-500` / `fake-slow` | 永远 500 / 先等 `--slow` 秒（测重试和超时） |
| `fake-503` | 永远返回空内容的 503，带 `Retry-After: 1`（看请求失败时诊断信息长什么样） |

```bash
python -m sgs.tools.fake_llm --port 8001
python -m sgs.arena configs/fake-model.yaml
```

`--delay 2` 让每个请求先等约 2 秒（`--jitter 0.5` 是上下浮动的比例），模拟真模型的生成时间，
用来看终端心跳和边打边写的文件。

`tests/test_agents.py` 就是用它测的重试、兜底、超时、并发和输出文件。

## 请求失败排查（例如 HTTP 503）

调用记录里看到 `"error": "请求失败（4 次）：HTTP 503: "`、响应体是空的，先跑一次自检：

```bash
python -m sgs.arena configs/你的配置.yaml --check
```

它会打印合并后的配置，然后对每个模型方：列一下服务端的模型名，发一次最小的对话请求，打印实际请求的地址、
**有没有走代理、走的哪个**、每次尝试的状态码、响应头（`Server`、`Via`、`Retry-After` 等）、响应体，
以及一句可能的原因。不通时退出码是 1。只想检查配置不发请求，加 `--no-connect`。

怎么判断是谁返回的 503：

| 看到的 | 多半是 | 怎么办 |
| --- | --- | --- |
| 走了代理，503，响应体为空，`Server` 是 Clash/mihomo/squid 之类 | 系统代理（`HTTP_PROXY` 等环境变量或系统设置）把请求转走了，代理连不上模型服务 | 本机和内网地址现在默认不走代理（`proxy: auto`）；其它地址设 `proxy: none` 或在 `NO_PROXY` 里加上 |
| 没走代理，503/502/504，响应体是 HTML，`Server` 是 nginx 之类 | 模型服务前面的网关，后面的服务没起来或超时 | 看模型服务是否在跑、是否过载 |
| 503/429，响应体是 JSON 报错，或带 `Retry-After` | 模型服务自己说忙、排队满了或限流 | 调小 `match.concurrency`，调大 `http_retries`、`backoff`；带 `Retry-After` 时会自动按它等 |
| `timeout` | 生成太慢或排队太久 | 调大 `timeout`，或降低并发 |
| `connect` | 地址或端口不对、服务没起来 | 检查 `base_url` |

这些信息也都在调用记录里：每次失败的请求有 `http` 字段（`url`、`proxy`、`proxy_mode`、每次尝试的 `status`、
`headers`、`body`、耗时和等待时间、`hint`），代理地址里的账号密码不会写进去。
`summary.json` 和 `index.html` 按错误类型统计了请求失败次数，终端每局结束也会打印。

我们在本机复现过一种：设置了 `http_proxy` 而 `no_proxy` 里没有 `localhost` 时，请求本机 vLLM 会被发给代理，
代理返回空内容的 503，错误信息正好是 `请求失败（3 次）：HTTP 503: `。之前的版本会这样，现在默认 `proxy: auto` 不再走代理。

## 扩展点（开闭原则）

都是“写新类 + 注册”，不用改已有代码：

- **新的智能体**：继承 `Agent` 实现 `choose(view, d)`，`@register_agent("名字")`；或者直接写 `模块:类名`。
  需要日志就设 `needs_history = True`（`view.logs` 是你能看到的全部日志文本），需要距离等派生量就设 `needs_derived = True`。
  每局开始和结束会调 `on_game_start(info)` / `on_game_end(info)`，可以在里面清空记忆。
- **新的协议**（非 OpenAI 格式的 API、gRPC、本地 transformers 直接推理）：继承 `ChatClient` 实现
  `chat(messages, **params) -> ChatResult`，`@register_client("名字")`，配置里写 `client=名字`。
- **新的提示词**（英文、更短、给训练用的固定模板、加牌面说明）：继承 `PromptBuilder` 改 `system()` / `state()` /
  `decision()` / `retry()` 其中之一，`@register_prompt("名字")`，配置里写 `prompt=名字`。
- **不是对话模型的策略**（例如训练出来的价值网络服务）：直接写一个 `Agent`，在 `choose` 里发 HTTP 请求，
  `view.to_dict()` 是 JSON 版的观察，`d.options[i].key` 是结构化的动作编码。
