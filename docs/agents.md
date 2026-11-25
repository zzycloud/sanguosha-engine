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

## 对战的输出

| 文件 | 内容 |
| --- | --- |
| `config.yaml` | 实际使用的完整配置（合并默认值和覆盖，密钥打码） |
| `summary.json` | A、B 各自胜场、先手胜场；模型方另有调用次数、不合法输出次数和比例、兜底次数、平均耗时、token 用量 |
| `games.jsonl` | 每局一行：种子、武将、A/B 坐哪、结果、用时、出错信息、录像文件名 |
| `decisions.jsonl` | 每个决策点一行，格式同 `sgs.export`（`sgs-decision/1`），多一个 `agent_info`：来源（模型 / 兜底）、重试次数、模型给的理由 |
| `llm_calls.jsonl` | 模型每次决策的完整对话（见下） |
| `replays/*.html` | 录像，浏览器直接打开 |
| `cards.json` | 牌 id 对照表 |

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
| `http_retries` / `backoff` | 2 / 1.0 | 429、5xx、超时、连不上时重试几次，指数退避的基数（秒） |
| `temperature` / `top_p` / `max_tokens` / `stop` / `sample_seed` | 0.7 / - / - / - / - | 采样参数，没填的不发 |
| `prompt` | `default` | 提示词模板名或 `模块:类名` |
| `history_lines` | 40 | 提示词里带最近多少条可见日志（0 不带） |
| `ask_reason` | false | 要求模型输出 `{"reason": ..., "action": ...}`，理由写进数据 |
| `system_prompt` / `extra_rules` | | 替换系统提示 / 在系统提示后追加文字（例如策略提示） |
| `show_skill_text` | true | 附上场上技能和装备的原文 |
| `max_retries` | 2 | 输出不合法时，把错误告诉模型再问几次 |
| `decision_timeout` | 不限 | 一个决策点总共最多花几秒（含重试），超过就兜底 |
| `fallback` | `rule` | 兜底智能体；`none` 表示不兜底、直接中止这局 |
| `skip_single` | true | 只有一个选项时不调模型 |
| `label` | `llm:模型名` | 统计和数据里显示的名字 |

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
6. **记录**：整个过程写一条 `llm_calls.jsonl`。

`llm_calls.jsonl` 每行（`sgs-llm-call/1`）：

| 字段 | 说明 |
| --- | --- |
| `game_id` / `seat` / `step` / `turn` / `phase` | 哪局、哪个座位、第几个决策 |
| `agent` / `model` | |
| `kind` / `prompt` / `legal_actions` | 决策类型、提示、全部合法动作文字 |
| `attempts` | 每次请求一项：`messages`（完整对话）、`request`（采样参数，不含密钥）、`response`（服务端原始返回）、`text`、`reasoning`（服务端单独给的思考内容）、`latency`、`usage`、`parsed`、`error` |
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

```bash
python -m sgs.tools.fake_llm --port 8001
python -m sgs.arena configs/fake-model.yaml
```

`tests/test_agents.py` 就是用它测的重试、兜底、超时、并发和输出文件。

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
