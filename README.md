# OutboundEval OS

本仓库按 `OutboundEval_最终实现方案.md` 实现一个本地可运行的复杂外呼任务指令遵循评测平台。

## 模块对应说明

- 第 5-7 章：`outbound_eval/domain` 与 `outbound_eval/compiler`。参考 OpenSpec 的 requirement block parser / validator，迁移为 Markdown 分段、Pydantic schema、规则校验，不照搬 TS schema。
- 第 8 章：`outbound_eval/spec_qa`。参考 BMAD triage 的 normalize / dedupe / classify 结构，输出本项目的 `SpecFinding`。
- 第 9 章：`outbound_eval/skills` 与 `eval_skills/*`。参考 Anthropic/BMAD skill catalog 形态，`rubric.yaml`、`scenario_templates.yaml`、`faq.md` 作为真源。
- 第 10-12 章：`outbound_eval/planner` 与 `outbound_eval/simulator`。参考 BrowserUse action registry，把用户行为 schema 化。
- 第 13-14 章：`outbound_eval/runner` 与 `outbound_eval/trace`。参考 AutoGPT harness 和 agentmemory timeline，支持 attempts、event stream、replay 查询。
- 第 15-19 章：`outbound_eval/evaluator`、`outbound_eval/scoring`、`outbound_eval/reporting`。JSON 报告是真源，Markdown/HTML 由同一份 `ReportArtifact` 渲染。
- 第 20-24 章：`outbound_eval/storage`、`outbound_eval/badcase`、`outbound_eval/golden`、`outbound_eval/cli.py`、`outbound_eval/web`。

## 快速开始

```powershell
E:\Anaconda\envs\OutboundEval\python.exe -m outbound_eval.cli compile --input samples\rider_contract.md --out runs\compiled
E:\Anaconda\envs\OutboundEval\python.exe -m outbound_eval.cli plan --task-spec runs\compiled\task_spec.json --out runs\planned
E:\Anaconda\envs\OutboundEval\python.exe -m outbound_eval.web.app
```

CLI 入口在安装后也可使用：

```powershell
outbound-eval model test --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --api-key "***" --model-name qwen-turbo
```

## 存储

正式路径默认使用 PostgreSQL + Redis：

```powershell
$env:PG_DSN="postgresql://agent_hub:agent_hub_pass@192.168.111.134:5432/outboundeval"
$env:REDIS_URL="redis://192.168.111.134:6379/0"
E:\Anaconda\envs\OutboundEval\python.exe -m outbound_eval init-db
```

PostgreSQL 表覆盖方案要求的 `task_definitions`、`task_specs`、`scenario_definitions`、`evaluation_runs`、`episode_executions`、`turn_events`、`judge_events`、`score_items`、`report_artifacts`、`badcase_items`、`golden_cases`、`golden_labels`，并额外包含 `trace_events`。Redis 用于运行状态与模型连接测试缓存。

SQLite 只保留为单元测试和离线本地验证 fallback，通过 `OUTBOUNDEVAL_STORAGE=sqlite` 或 CLI 的 `--sqlite-db` 显式启用；正式交付不走 SQLite。
