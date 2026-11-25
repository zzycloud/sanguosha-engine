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

## 一行字符串指定“谁来打”

命令行和 Python 里都用同一种写法：

| 写法 | 含义 |
| --- | --- |
| `rule` / `random` | 内置规则 AI / 随机 AI |
| `llm?model=M&base_url=URL` | OpenAI 兼容接口的模型，`?` 后面是配置，`&` 分隔 |
| `llm@configs/x.json` | 配置放在 JSON 文件里 |
| `llm@configs/x.json?temperature=0` | 文件 + 临时覆盖 |
| `mypkg.bots:MyAgent?depth=3` | 不注册，直接用你自己的类 |

```python
from sgs.agents import make_agent
ag = make_agent("llm?model=qwen&base_url=http://localhost:8000/v1&temperature=0.2", seed=0)
```

## 对战命令行

```bash
python -m sgs.arena --p0 <A 的写法> --p1 <B 的写法> --games 20 -j 4 --swap -o runs/名字
```

| 参数 | 说明 |
| --- | --- |
| `--games` / `--seed` | 局数；第一局的种子 |
| `--generals 关羽 吕布` | 固定两个座位的武将，不填每局随机（标准包） |
| `--swap` | 每两局用同一副牌、同一对武将，交换先后手（1v1 先手优势明显，比胜率时建议开） |
| `-j/--concurrency` | 同时进行的对局数。接模型时调大，让推理服务的批处理吃满 |
| `--game-timeout` | 单局最多几秒，超时中止并记为“超时” |
| `--replays` | 录几局录像：数字 / `all` / `none`（默认 5） |
| `--rollouts` / `--history` / `--no-privileged` | 同 `sgs.export`，见 [data.md](data.md) |

输出目录：

| 文件 | 内容 |
| --- | --- |
| `summary.json` | A、B 各自胜场、先手胜场；模型方另有调用次数、不合法输出次数和比例、兜底次数、平均耗时、token 用量 |
| `games.jsonl` | 每局一行：种子、武将、A/B 坐哪、结果、用时、出错信息、录像文件名 |
| `decisions.jsonl` | 每个决策点一行，格式同 `sgs.export`（`sgs-decision/1`），多一个 `agent_info`：来源（模型 / 兜底）、重试次数、模型给的理由 |
| `llm_calls.jsonl` | 模型每次决策的完整对话（见下） |
| `replays/*.html` | 录像，浏览器直接打开 |
| `cards.json` | 牌 id 对照表 |

Python 里也可以直接调：`from sgs.arena import run_arena; run_arena("rule", "llm?...", games=10, out="runs/x")`。

## 接你自己部署的模型

先用推理框架起一个 OpenAI 兼容服务，然后把地址填进 `base_url`。

```bash
# vLLM
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8000
python -m sgs.arena --p0 "llm?model=Qwen/Qwen2.5-7B-Instruct&base_url=http://localhost:8000/v1" --p1 rule -j 8

# SGLang
python -m sglang.launch_server --model-path Qwen/Qwen2.5-7B-Instruct --port 30000
python -m sgs.arena --p0 "llm?client=sglang&model=Qwen/Qwen2.5-7B-Instruct" --p1 rule -j 8

# Ollama（OpenAI 兼容地址是 /v1）
ollama run qwen2.5:7b
python -m sgs.arena --p0 "llm?client=ollama&model=qwen2.5:7b" --p1 rule -j 2

# 你自己用 LoRA 微调后的模型：vLLM 加 --enable-lora --lora-modules sgs=/path/to/lora，然后 model=sgs
```

`client=vllm/sglang/ollama` 只是默认地址不同（8000 / 30000 / 11434），写了 `base_url` 就以它为准。

## 接商用 API

任何提供 OpenAI 兼容 `/chat/completions` 的服务都能直接用。**api_key 建议放在环境变量里**，
不要写进命令行或提交到仓库的配置文件：

```bash
export MY_API_KEY=...         # 不会写进任何输出文件
python -m sgs.arena --p0 "llm@configs/api.json" --p1 rule -j 4
```

```json
{
  "kind": "llm",
  "base_url": "https://你的服务商/v1",
  "model": "模型名",
  "api_key_env": "MY_API_KEY",
  "temperature": 0.3,
  "max_tokens": 512,
  "timeout": 60,
  "max_retries": 2,
  "ask_reason": true
}
```

付费 API 会按 token 收费：一个决策点的提示词约 1000～2000 字，一局 30～80 个决策点。
先用 `--games 2` 看 `summary.json` 里的 token 统计再放大。

## 全部配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `client` | `openai` | 协议：`openai` / `vllm` / `sglang` / `ollama` 或 `模块:类名` |
| `base_url` | 各 client 的默认地址 | 到 `/v1` 为止 |
| `model` | | 发给服务端的模型名 |
| `api_key` / `api_key_env` | 无 / `OPENAI_API_KEY` | 直接给密钥，或从哪个环境变量读；本地服务通常不需要 |
| `headers` / `extra_body` | | 额外请求头 / 合并进请求体的字段（如 vLLM 的 `guided_json`、`top_k`，或关闭思考的开关） |
| `timeout` | 60 | 单次请求最多等几秒 |
| `http_retries` / `backoff` | 2 / 1.0 | 429、5xx、超时、连不上时重试几次，指数退避的基数（秒） |
| `temperature` / `top_p` / `max_tokens` / `stop` / `sample_seed` | 0.7 / - / - / - / - | 采样参数，没填的不发 |
| `prompt` | `default` | 提示词模板名或 `模块:类名` |
| `history_lines` | 40 | 提示词里带最近多少条可见日志（0 不带） |
| `ask_reason` | false | 要求模型输出 `{"reason": ..., "action": ...}`，理由写进数据 |
| `system_prompt` / `extra_rules` | | 替换开头的说明 / 在说明后追加文字（例如策略提示） |
| `show_skill_text` | true | 附上场上技能和装备的原文 |
| `max_retries` | 2 | 输出不合法时，把错误告诉模型再问几次 |
| `decision_timeout` | 不限 | 一个决策点总共最多花几秒（含重试），超过就兜底 |
| `fallback` | `rule` | 兜底智能体（任意写法）；`none` 表示不兜底、直接报错中止这局 |
| `skip_single` | true | 只有一个选项时不调模型 |
| `transcript` | | 单独使用时把调用记录写到这个 JSONL；用 arena 时自动写到 `llm_calls.jsonl` |
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
python -m sgs.arena --p0 "llm?model=fake-flaky&base_url=http://127.0.0.1:8001/v1" --p1 rule --games 4 -j 4
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
