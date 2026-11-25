# 配置文件怎么看

所有配置都是 YAML，一行一项，缩进表示“属于上面那一项”。

## 目录里有什么

| 位置 | 放什么 |
|---|---|
| `models/` | **模型**。一个模型一个文件：地址、模型名、密钥从哪读、思考强度、max_tokens。 |
| `tips/` | **武将技巧**。一个武将一个 `.md`，开了 `tips: true` 就把选到的武将的技巧放进提示词。 |
| `compare-*.yaml` | **对比实验**。同一个模型换几种设置各打若干局，比胜率。 |
| `*.yaml`（其它） | **普通对战**。A 和 B 打若干局。 |
| `data/` | **造训练数据**和**导出数据**的配置。 |

现在有的模型文件：

- `models/step-5-preview.yaml`：阶跃 step-5-preview（云 API，要密钥）。
- `models/qwen-local.yaml`：本机部署的模型（vLLM 等，不用密钥），改成你的 8B。
- `models/fake.yaml`：本地假模型，不花钱，只看流程通不通。

要加别的模型：复制一个文件，改 `label`、`base_url`、`model`、`api_key_env`。

## 密钥放哪

不写进文件（仓库是公开的）。模型文件里写的是“从哪个环境变量读”：

```yaml
api_key_env: STEPFUN_API_KEY
```

跑之前在终端里设一次：

```bash
export STEPFUN_API_KEY=你的密钥        # Windows PowerShell：$env:STEPFUN_API_KEY="你的密钥"
```

这个终端窗口关掉就没了；想一直有，把这行写进 `~/.bashrc` 或 `~/.zshrc`。

## A 和 B 怎么用模型

对战双方叫 A 和 B。每一方要么是规则 AI，要么引用一个模型文件：

```yaml
players:
  A:
    include: models/step-5-preview.yaml   # 用这个模型
    prompt: v2                             # 下面还能接着写，盖掉模型文件里的同名项
    reasoning_effort: medium
  B: rule                                  # 规则 AI
```

想让模型对模型，把 B 也写成 `include: models/...`。`include` 的路径相对当前这个配置文件。

## 思考强度

写在模型文件里：`reasoning_effort: low`（可选 low / medium / high）。
某个实验想换，在那个实验的 A 下面再写一行 `reasoning_effort: high` 就行，模型文件不用改。

## 对比实验里的 base 和 variants

```yaml
base:          # 几组共用的设置：打几局、A 和 B 是谁
  ...
variants:      # 每一组在 base 上改的地方
  none:        # 组名，结果放在同名子目录里
    players:
      A:
        label: 无技巧无分析
  tips:
    players:
      A:
        label: 技巧
        tips: true
```

每组用同一批种子：第 k 局双方武将和牌堆完全一样，只有你在 variants 里改的那几项不同，所以胜率差就是那几项带来的。
跑完看总目录里的 `comparison.md`。

## 临时改一项

不用改文件，命令后面加 `--set`：

```bash
uv run python -m sgs.experiment configs/compare-tips-cot.yaml --set base.match.games=50
uv run python -m sgs.arena configs/cloud-api.yaml --set players.A.reasoning_effort=high
```

## 常用命令

```bash
# 四组对比（技巧 / 短分析），花钱
export STEPFUN_API_KEY=你的密钥
uv run python -m sgs.experiment configs/compare-tips-cot.yaml

# 同样四组用假模型试跑，不花钱（两个终端）
uv run python -m sgs.tools.fake_llm --port 8001
uv run python -m sgs.experiment configs/compare-tips-cot-fake.yaml

# 把跑出来的对局导出成训练数据
uv run python -m sgs_data.cot_export configs/data/cot-export.yaml
```

所有能写的项和默认值见 `local-vllm.yaml`（列全了）和 `docs/agents.md`。
