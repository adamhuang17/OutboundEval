# OutboundEval OS

OutboundEval OS 是一个本地可运行的复杂外呼任务指令遵循评测平台。它把 Markdown 外呼任务说明编译为结构化 `TaskUnderstanding` / `TaskSpec`，生成覆盖场景，驱动模拟用户与目标外呼模型多轮对话，再用规则、知识、流程、约束、异常处理和语义评审产出评分、证据、风险防护和改进报告。

当前仓库的样例围绕美团外卖业务展开：

- `samples/rider_contract.md`：骑手合同外呼任务样例。
- `samples/merchant_live_config.md`：商家直播配置外呼任务样例。
- `eval_skills/rider_contract` 与 `eval_skills/merchant_live_config`：任务技能包、场景模板、评分规则、FAQ 和 persona 配置。

## 代码主链路

1. 指令理解与编译：`outbound_eval/compiler` 解析 Markdown，构建 AST，经 staged LLM compiler 生成任务大纲、流程、知识、约束、需求与 judge plan，并落到 `TaskUnderstanding` / `TaskSpec`。
2. 规格 QA：`outbound_eval/spec_qa` 做完整性、歧义和风险审计，风险规则来自 `risk_taxonomy.yaml`，不会依赖某个硬编码业务词。
3. 场景规划：`outbound_eval/planner` 根据需求、流程、FAQ、约束和风险覆盖要求生成 `CoverageMatrix` 与 `ScenarioSpec`。
4. 对话执行：`outbound_eval/runner` 与 `outbound_eval/simulator` 驱动用户模拟器和目标模型对话，`visibility_filter` 防止隐藏评测目标泄漏给目标模型。
5. 评测打分：`outbound_eval/evaluator` 聚合规则检查、流程检查、知识检查、约束检查、异常检查和语义 judge，`outbound_eval/scoring` 统一计算分数和 severity cap。
6. 报告沉淀：`outbound_eval/reporting` 生成 `report.json`、`report.md`、`report.html`，`storage` 与 `trace` 负责 PostgreSQL/SQLite 持久化和事件追踪。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `outbound_eval/domain` | Pydantic 领域模型：任务、场景、对话、judge、评分、报告等 schema。 |
| `outbound_eval/compiler` | Markdown AST、LLM 分阶段编译、TaskSpec 校验和 compile QA。 |
| `outbound_eval/llm` | 结构化 LLM 客户端，支持 `response_format=json_object` 和纯 JSON fallback。 |
| `outbound_eval/adapters` | OpenAI-compatible Chat Completions 适配器。 |
| `outbound_eval/spec_qa` | 指令规格审计、风险检测、guard contract 和 triage。 |
| `outbound_eval/planner` | 覆盖矩阵、风险场景、LLM 场景计划和场景修复。 |
| `outbound_eval/simulator` | 模拟用户、对话管理、动作注册和可见性过滤。 |
| `outbound_eval/evaluator` | 规则、流程、知识、约束、异常和语义评测器。 |
| `outbound_eval/runner` | episode/batch run、复评服务。 |
| `outbound_eval/reporting` | JSON、Markdown、HTML 报告生成。 |
| `outbound_eval/storage` | PostgreSQL 正式存储和 SQLite 本地 fallback。 |
| `outbound_eval/trace` | turn、model_call、judge 等追踪事件。 |
| `outbound_eval/web` | FastAPI 后端和静态前端页面。 |
| `tests` | 结构化客户端、验收链路、风险防护覆盖测试。 |

## 安装

要求 Python `>=3.11`。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

安装后可以使用两种入口：

```powershell
python -m outbound_eval.cli --help
outbound-eval --help
```

## 模型配置

项目使用 OpenAI-compatible Chat Completions API。所有 CLI 中需要模型的命令都显式传入：

- `--base-url`：兼容 OpenAI 的 `/v1` 或供应商兼容路径。
- `--api-key`：模型 API key。
- `--model-name`：模型名称。
- `--temperature`：默认 `0.2`。
- `--max-tokens`：默认 `512`，编译复杂任务建议调到 `4096` 或更高。
- `--timeout-seconds`：默认 `30`，编译链路建议 `60`。

PowerShell 示例：

```powershell
$env:OE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:OE_API_KEY="sk-..."
$env:OE_MODEL="qwen-turbo"
```

先测试模型连通性：

```powershell
python -m outbound_eval.cli model test `
  --base-url $env:OE_BASE_URL `
  --api-key $env:OE_API_KEY `
  --model-name $env:OE_MODEL
```

## CLI 快速开始

### 1. 编译外呼指令

`compile` 会把 Markdown 指令编译为 `task_spec.json`、`task_understanding.json` 和 `validation_report.json`。当前代码中 `InstructionCompileService.compile()` 已经禁用旧的纯规则抽取路径，必须提供模型配置。

```powershell
python -m outbound_eval.cli compile `
  --input samples\rider_contract.md `
  --out runs\rider_compile `
  --base-url $env:OE_BASE_URL `
  --api-key $env:OE_API_KEY `
  --model-name $env:OE_MODEL `
  --max-tokens 4096 `
  --timeout-seconds 60
```

### 2. 生成覆盖场景

`plan` 只消费已编译出的 `TaskSpec`，不需要模型。

```powershell
python -m outbound_eval.cli plan `
  --task-spec runs\rider_compile\task_spec.json `
  --budget 12 `
  --out runs\rider_plan
```

输出包括：

- `coverage_matrix.json`
- 每个场景一份 `scn.*.json`

### 3. 完整运行评测

正式路径默认使用 PostgreSQL + Redis。先配置并初始化：

```powershell
$env:PG_DSN="postgresql://agent_hub:agent_hub_pass@127.0.0.1:5432/outboundeval"
$env:REDIS_URL="redis://127.0.0.1:6379/0"

python -m outbound_eval.cli init-db `
  --pg-dsn $env:PG_DSN `
  --redis-url $env:REDIS_URL
```

运行端到端评测：

```powershell
python -m outbound_eval.cli run `
  --instruction samples\rider_contract.md `
  --base-url $env:OE_BASE_URL `
  --api-key $env:OE_API_KEY `
  --model-name $env:OE_MODEL `
  --max-tokens 4096 `
  --timeout-seconds 60 `
  --budget 12 `
  --attempts 1 `
  --parallel 1 `
  --out-dir runs\rider_run `
  --pg-dsn $env:PG_DSN `
  --redis-url $env:REDIS_URL
```

本地调试可以把主数据和 trace 写到 SQLite：

```powershell
python -m outbound_eval.cli run `
  --instruction samples\rider_contract.md `
  --base-url $env:OE_BASE_URL `
  --api-key $env:OE_API_KEY `
  --model-name $env:OE_MODEL `
  --max-tokens 4096 `
  --timeout-seconds 60 `
  --budget 12 `
  --out-dir runs\rider_sqlite `
  --redis-url $env:REDIS_URL `
  --sqlite-db runs\outbound_eval.db
```

注意：`--sqlite-db` 只切换 repository 和 trace store；CLI `run` 仍会用 Redis 做模型连通性缓存和运行状态写入。

### 4. 查看报告

一次 `run` 会在 `--out-dir` 下写出：

- `connection_test.json`
- `task_spec.json`
- `task_understanding.json`
- `validation_report.json`
- `qa_result.json`
- `coverage_matrix.json`
- `target_request_payloads.jsonl`
- `episodes.jsonl`
- `judge_events.jsonl`
- `badcases.json`
- `report.json`
- `report.md`
- `report.html`

也可以把已有 `report.json` 渲染成 Markdown 输出到终端：

```powershell
python -m outbound_eval.cli report --report-json runs\rider_run\report.json
```

## Web 服务

Web 入口基于 FastAPI，前端静态资源在 `outbound_eval/web/static`。

```powershell
$env:OUTBOUNDEVAL_STORAGE="sqlite"
$env:REDIS_URL="redis://127.0.0.1:6379/0"
python -m outbound_eval.web.app
```

然后访问：

```text
http://127.0.0.1:8000
```

Web 端支持四类模型角色：

- `compiler_model`：任务理解与编译。
- `target_model`：被测外呼模型。
- `simulator_model`：模拟被叫用户。
- `judge_model`：语义评分模型。

常用 API 流程：

1. `POST /api/models/test-all`：同时测试四个模型角色。
2. `POST /api/task/understand/start`：启动后台编译。
3. `GET /api/task/understand/{compile_id}/events`：SSE 获取编译阶段进度。
4. `GET /api/task/understand/{compile_id}/result`：获取 `TaskUnderstanding`。
5. `POST /api/scenarios/build`：生成并修复场景集。
6. `POST /api/run/start`：启动评测 run。
7. `GET /api/run/{run_id}/events`：SSE 获取 run 进度和对话 turn。
8. `GET /api/run/{run_id}/result`：获取报告数据。
9. `GET /api/report/{run_id}/html`：查看 HTML 报告。

旧接口 `/api/compile`、`/api/plan`、`/api/run` 在当前代码中返回 `410`，默认产品路径应使用上面的新接口。

## 存储与状态

正式交付默认使用 PostgreSQL + Redis：

- PostgreSQL 表包括 `task_definitions`、`task_understandings`、`compile_stage_results`、`compile_artifacts`、`compile_diagnostics`、`persona_profiles`、`task_specs`、`scenario_definitions`、`evaluation_runs`、`episode_executions`、`turn_events`、`judge_events`、`score_items`、`report_artifacts`、`badcase_items`、`golden_cases`、`golden_labels` 和 `trace_events`。
- Redis 用于运行状态和模型连接测试缓存。
- SQLite 是本地调试和测试 fallback，通过 `OUTBOUNDEVAL_STORAGE=sqlite` 或 CLI `--sqlite-db` 显式启用。

默认配置在 `outbound_eval/config.py`：

- `PG_DSN`
- `REDIS_URL`
- `OUTBOUNDEVAL_STORAGE`

## 评测能力

当前代码覆盖以下评测维度：

- 开场白、禁用话术、承诺奖励等规则检查。
- 流程节点和分支条件覆盖。
- FAQ 与知识事实正确性。
- 约束遵守和异常处理。
- 风险 taxonomy 检测、guard contract、风险场景覆盖和 severity cap。
- Web 新链路中的 LLM 语义 judge，基于 `JudgePlan` 逐项给出 verdict、证据 turn 和改进建议。

CLI `run` 当前使用同一份模型配置串起编译、目标模型和模拟用户，并主要依赖内置 checker 产出 judge events；Web 新链路支持 compiler、target、simulator、judge 四模型分离。

## 测试与轻量校验

轻量校验：

```powershell
python -m outbound_eval.cli --help
python -m unittest tests.test_structured_client
```

全量测试在 `tests` 下，覆盖验收链路、结构化 JSON fallback 和风险防护覆盖。涉及编译链路的测试需要与当前 `InstructionCompileService` 的模型配置要求保持一致。
