# Harness 设计：从配置文件到一局对战

整体分六层，上层只依赖下层的接口；每层的扩展都是“写新类 + 注册”，不改已有代码。

```
 configs/xxx.yaml
      │  sgs/config.py：读文件、合并默认值和 --set、检查字段、展开 ${环境变量}
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 6 输出      summary.json · games.jsonl · decisions.jsonl · llm_calls.jsonl │  ← output 段
│             replays/*.html · config.yaml                                   │
├──────────────────────────────────────────────────────────────────────────┤
│ 5 对战调度  sgs/arena.py：排种子和座位（换边）、线程池并发、单局超时、         │  ← match 段
│             单局出错隔离、汇总胜率和模型统计                                  │
├──────────────────────────────────────────────────────────────────────────┤
│ 4 LLM 适配  sgs/agents/llm/：PromptBuilder 渲染提示词 → ChatClient 调接口 →   │  ← players.X
│             parse_action 解析编号 → 不合法重问 → 失败兜底 → 写调用记录         │    （agent: llm）
├──────────────────────────────────────────────────────────────────────────┤
│ 3 智能体    Agent.choose(view, decision) -> 编号；注册表把名字变成实例：        │  ← players.X.agent
│             rule / random / llm / 你注册的名字 / 模块:类名                     │
├──────────────────────────────────────────────────────────────────────────┤
│ 2 观察接口  decide(agent, g)：只交出 PlayerView（决策者视角快照）和 Decision    │
│             （全部合法动作，已过规则检查）；智能体拿不到 Game，看不到对手手牌   │
├──────────────────────────────────────────────────────────────────────────┤
│ 1 引擎      sgs/core + sgs/packs：显式帧栈的规则引擎，任何时刻停在一个决策点；   │
│             可 clone / determinize（rollout）；卡牌、技能、武将、模式都注册进来 │
└──────────────────────────────────────────────────────────────────────────┘
```

## 一局里的调用流程

```
arena.play_one(种子, 座位)
  ├─ make_agent(players.A) / make_agent(players.B)     每局新建，互不共享状态
  ├─ Recorder.new_game(mode, seed, generals)           引擎每写一条日志，录像多一步
  └─ while 没结束:
       d = g.pending                                   引擎停在一个决策点
       只有一个选项 → 直接选
       否则:
         row = DecisionLog.before()                    记下决策者视角（导出用）
         i = decide(agents[d.player], g)
               └─ agent.choose(g.view(座位), d)
                    LLMAgent:
                      messages = PromptBuilder.messages(view, d)
                      for 第 k 次 (≤ max_retries+1):
                          r = ChatClient.chat(messages)          网络错误、429、5xx 在这里重试
                          i = parse_action(r.text, len(d.options))
                          合法 → 结束；不合法 → 追加“你的回答不合法……”再问
                      仍失败 → fallback.choose(view, d)          默认规则 AI
                      写一条 llm_calls.jsonl
         DecisionLog.after(row, i, agent.last_info)
         Recorder.act(i)  →  g.act(i)                  引擎结算，直到下一个决策点
  └─ 写 decisions.jsonl、录像，返回这局的汇总
```

## 每层的要点

1. **引擎**：规则全部在引擎里，包括“锁定技强制、非锁定技必问”这类通用规则；对外只暴露“当前决策点 + 选第几个”。
   状态可以复制，所以 rollout、MCTS、数据标注都在同一套引擎上做。
2. **观察接口**：`PlayerView` 是快照，不持有 `Game`，看不见的牌一查就报错，从结构上防止“偷看”。
   `view.to_dict()` 是 JSON 版，`d.options[i].key` 是结构化动作编码，`label` 是给人和模型看的文字。
3. **智能体**：只有一个方法 `choose(view, d)`，外加可选的每局开始 / 结束钩子。
   需要日志、距离等额外信息时声明 `needs_history` / `needs_derived`，引擎才去算。
4. **LLM 适配**：三个可替换部件——`ChatClient`（协议）、`PromptBuilder`（提示词）、`parse_action`（解析）。
   `LLMAgent` 负责把它们串起来，加上重问、超时、兜底和记录。换协议、换模板都在配置里写名字。
5. **对战调度**：线程池并发（大模型推理是 I/O 等待，线程就够），每局独立建智能体，一局出错只记录不影响别的局；
   换边用复式赛制（同一种子交换座位）。
6. **输出**：录像、训练数据、模型原始对话三份数据用同一个 `game_id` + `step` 关联，可以互相对照。

## 配置文件怎么映射到组件

| 配置 | 去了哪里 |
| --- | --- |
| `match.mode / seed / generals / general_pool` | 建 `Game(mode, seed, generals)` |
| `match.games / swap / concurrency / game_timeout / max_steps` | `arena.run_arena` 的调度参数 |
| `players.X.agent` | 注册表 `make_agent` 选哪个智能体类 |
| `players.X` 其它字段 | 原样传给该智能体的构造函数（`llm` 的字段分给 `ChatClient`、`PromptBuilder` 和 `LLMAgent` 自己） |
| `players.X.client / prompt` | `ChatClient` / `PromptBuilder` 注册表里的名字 |
| `output.*` | 输出目录、录像局数、是否写 decisions / llm_calls、rollout 估值和导出字段 |

## 扩展点

| 想做的事 | 写什么 | 配置里怎么用 |
| --- | --- | --- |
| 新的 AI（MCTS、价值网络服务……） | 继承 `Agent`，`@register_agent("mcts")` | `agent: mcts` 或 `agent: 包.模块:类名` |
| 新的模型接口协议 | 继承 `ChatClient`，`@register_client("myproto")` | `client: myproto` |
| 新的提示词模板 | 继承 `PromptBuilder`，`@register_prompt("short")` | `prompt: short` |
| 新的卡牌、技能、武将、模式 | 见 [design.md](design.md) | `match.mode`、`match.generals` |
