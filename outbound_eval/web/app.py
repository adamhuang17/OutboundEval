from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.adapters import OpenAICompatibleAdapter
from outbound_eval.badcase import BadcaseLibrary
from outbound_eval.compiler import InstructionCompileService
from outbound_eval.compiler.llm_task_compiler import LLMTaskCompiler
from outbound_eval.config import settings
from outbound_eval.domain.schemas_episode import EpisodeExecution, TurnEvent
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_persona import EvaluatorPersonaInput
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.domain.schemas_understanding import (
    ScenarioSet,
    ScenarioSpec as LLMScenarioSpec,
    TaskUnderstanding,
)
from outbound_eval.golden import GoldenSetService
from outbound_eval.planner import CoveragePlanner
from outbound_eval.planner.scenario_builder_llm import ScenarioBuilderLLM
from outbound_eval.reporting import ReportGenerator
from outbound_eval.runner import BatchRunner
from outbound_eval.scoring import ScoreAggregator
from outbound_eval.runner.rejudge import RejudgeService
from outbound_eval.spec_qa import SpecQAService
from outbound_eval.status import RedisStateStore
from outbound_eval.storage import PostgresRepository, default_repository
from outbound_eval.trace import PostgresTraceStore


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="OutboundEval OS")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
repo = default_repository()
redis_state = RedisStateStore(settings().redis_url)

# In-memory run state store (用于 SSE 推送)
_run_events: dict[str, list[dict]] = {}
_run_locks: dict[str, asyncio.Lock] = {}

# ---------- 请求/响应 schema ----------

class ThreeModelConfigs(BaseModel):
    """前端必须同时配置三个 LLM。"""
    compiler_model: ModelConfig
    simulator_model: ModelConfig
    judge_model: ModelConfig


class TestThreeModelsRequest(BaseModel):
    configs: ThreeModelConfigs


class CompileRequest(BaseModel):
    instruction: str


class LLMCompileRequest(BaseModel):
    instruction: str
    llm_config: ModelConfig


class BuildScenariosRequest(BaseModel):
    understanding: dict[str, Any]
    persona: dict[str, Any] = Field(default_factory=dict)
    scenario_count: int = 6
    llm_config: ModelConfig


class StartRunRequest(BaseModel):
    instruction: str
    understanding: dict[str, Any]
    scenarios: list[dict[str, Any]]
    compiler_model: ModelConfig
    simulator_model: ModelConfig
    judge_model: ModelConfig
    attempts: int = 1
    parallel: int = 1


class ImportConversationRequest(BaseModel):
    """导入已有对话 JSON。"""
    run_id: str | None = None
    scenario: dict[str, Any]
    turns: list[dict[str, Any]]
    judge_plan: dict[str, Any] | None = None
    task_spec: dict[str, Any] | None = None


class RejudgeImportedRequest(BaseModel):
    run_id: str
    judge_model: ModelConfig


class QARequest(BaseModel):
    instruction: str
    task_spec: dict[str, Any]


class PlanRequest(BaseModel):
    task_spec: dict[str, Any]
    budget: int = 12


class RejudgeRequest(BaseModel):
    task_spec: dict[str, Any]
    scenario: dict[str, Any]
    episode: dict[str, Any]


class RunRequest(BaseModel):
    instruction: str
    target_model_config: ModelConfig
    budget: int = 12
    attempts: int = 1
    parallel: int = 1


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


# ===== 新版 API =====

@app.post("/api/models/test-all")
async def test_all_models(request: TestThreeModelsRequest):
    """同时测试三个 LLM 配置，全部通过才返回 ok=True。"""
    adapter = OpenAICompatibleAdapter()
    results = await asyncio.gather(
        adapter.test_connection(request.configs.compiler_model),
        adapter.test_connection(request.configs.simulator_model),
        adapter.test_connection(request.configs.judge_model),
        return_exceptions=True,
    )
    compiler_r = results[0] if not isinstance(results[0], Exception) else None
    simulator_r = results[1] if not isinstance(results[1], Exception) else None
    judge_r = results[2] if not isinstance(results[2], Exception) else None

    def _fmt(r, label):
        if r is None:
            return {"role": label, "ok": False, "error": "exception"}
        return {"role": label, "ok": r.ok, "latency_ms": r.latency_ms, "error": r.error_message}

    all_ok = all(
        (r is not None and r.ok)
        for r in [compiler_r, simulator_r, judge_r]
    )
    return {
        "ok": all_ok,
        "details": [
            _fmt(compiler_r, "compiler"),
            _fmt(simulator_r, "simulator"),
            _fmt(judge_r, "judge"),
        ],
    }


@app.post("/api/model/test")
async def model_test(config: ModelConfig):
    return await OpenAICompatibleAdapter().test_connection(config)


@app.post("/api/task/understand")
async def task_understand(request: LLMCompileRequest):
    """LLM 编译任务：返回 TaskUnderstanding。"""
    try:
        compiler = LLMTaskCompiler()
        understanding = await compiler.compile(
            raw_markdown=request.instruction,
            model_config=request.llm_config,
        )
        repo.upsert_json(
            "task_understandings",
            understanding.task_spec.get("task_id", "unknown"),
            understanding.model_dump(mode="json"),
        )
        return {"ok": True, "understanding": understanding.model_dump(mode="json")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/scenarios/build")
async def build_scenarios(request: BuildScenariosRequest):
    """LLM 生成测试场景。"""
    try:
        understanding = TaskUnderstanding.model_validate(request.understanding)
        persona = EvaluatorPersonaInput.model_validate(request.persona) if request.persona else EvaluatorPersonaInput()
        builder = ScenarioBuilderLLM()
        scenario_set = await builder.build(
            understanding=understanding,
            persona=persona,
            scenario_count=request.scenario_count,
            model_config=request.llm_config,
        )
        return {"ok": True, "scenario_set": scenario_set.model_dump(mode="json")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/run/start")
async def run_start(request: StartRunRequest):
    """启动评测 run，返回 run_id；通过 SSE 实时推送进度。"""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    _run_events[run_id] = []
    _run_locks[run_id] = asyncio.Lock()

    async def _push(event: dict):
        async with _run_locks[run_id]:
            _run_events[run_id].append({**event, "ts": datetime.now(timezone.utc).isoformat()})

    async def _run():
        try:
            await _push({"type": "stage", "stage": "compile_task", "message": "正在编译任务..."})

            understanding = TaskUnderstanding.model_validate(request.understanding)
            scenarios_raw = request.scenarios
            judge_plan = understanding.judge_plan

            await _push({"type": "stage", "stage": "build_complete", "message": f"场景数量: {len(scenarios_raw)}"})

            from outbound_eval.domain.ids import timestamped_id
            from outbound_eval.domain.enums import EpisodeStatus, TurnRole
            from outbound_eval.domain.schemas_episode import TurnEvent as TE
            from outbound_eval.llm.structured_client import StructuredLLMClient
            from outbound_eval.evaluator.semantic_judge import SemanticJudge

            structured_client = StructuredLLMClient()

            all_judge_results = []
            all_episodes = []

            for scn_idx, scn_raw in enumerate(scenarios_raw):
                scn = LLMScenarioSpec.model_validate(scn_raw)
                episode_id = timestamped_id("ep")
                task_id = understanding.task_spec.get("task_id", "task_unknown")

                await _push({
                    "type": "episode_start",
                    "episode_id": episode_id,
                    "scenario_id": scn.scenario_id,
                    "scenario_title": scn.title,
                    "scn_index": scn_idx,
                    "total": len(scenarios_raw),
                })

                turns: list[TE] = []

                # Initial user utterance
                user_content = scn.initial_user_utterance or "您好"
                user_turn = TE(
                    id=timestamped_id("turn"),
                    run_id=run_id,
                    episode_id=episode_id,
                    turn_index=0,
                    role=TurnRole.USER,
                    content=user_content,
                )
                turns.append(user_turn)
                await _push({
                    "type": "turn",
                    "episode_id": episode_id,
                    "turn_id": user_turn.id,
                    "role": "user",
                    "content": user_content,
                    "turn_index": 0,
                })

                # System prompt for target
                task_spec = understanding.task_spec
                system_prompt = (
                    f"你是{task_spec.get('role', '外呼客服')}。\n"
                    f"任务目标：{task_spec.get('objective', '')}\n"
                    f"开场白：{task_spec.get('opening_line', '')}\n\n"
                    f"任务说明：\n{request.instruction}"
                )

                max_turns = min(scn.max_turns, 12)
                should_continue = True

                for turn_idx in range(1, max_turns * 2):
                    if not should_continue:
                        break

                    # Target LLM reply
                    target_messages = [{"role": "system", "content": system_prompt}]
                    for t in turns:
                        role_map = {"user": "user", "assistant": "assistant", "system": "system"}
                        target_messages.append({
                            "role": role_map.get(str(t.role), "user"),
                            "content": t.content,
                        })

                    try:
                        target_reply = await structured_client.invoke_text(
                            model_config=request.simulator_model,  # target uses simulator model slot here (configurable)
                            messages=target_messages,
                            temperature=0.3,
                        )
                    except Exception as exc:
                        target_reply = f"[模型调用失败: {exc}]"
                        should_continue = False

                    assistant_turn = TE(
                        id=timestamped_id("turn"),
                        run_id=run_id,
                        episode_id=episode_id,
                        turn_index=len(turns),
                        role=TurnRole.ASSISTANT,
                        content=target_reply,
                    )
                    turns.append(assistant_turn)
                    await _push({
                        "type": "turn",
                        "episode_id": episode_id,
                        "turn_id": assistant_turn.id,
                        "role": "assistant",
                        "content": target_reply,
                        "turn_index": len(turns) - 1,
                    })

                    if not should_continue:
                        break

                    # User Simulator LLM
                    from pydantic import BaseModel as _BM, ConfigDict as _CD, Field as _F
                    class _SimOut(_BM):
                        model_config = _CD(extra="allow")
                        utterance: str = ""
                        should_continue: bool = True
                        state: str = "active"
                        intent: str = ""

                    sim_transcript = "\n".join(
                        f"[{t.role}]: {t.content}" for t in turns
                    )
                    sim_messages = [
                        {"role": "system", "content": (
                            "你是模拟用户，正在进行电话沟通。\n"
                            f"你的画像：{scn.persona.model_dump()}\n"
                            f"你的目标：{scn.user_goal}\n"
                            f"对话方向：{'; '.join(scn.dialogue_direction)}\n"
                            "规则：\n"
                            "- 每次输出一句自然话术，不超过30字\n"
                            "- 不泄露测试目的\n"
                            "- 当目标已达成或对话自然结束时，should_continue=false\n"
                            "输出 JSON: {utterance, should_continue, state, intent}"
                        )},
                        {"role": "user", "content": f"当前对话:\n{sim_transcript}\n\n请输出下一句用户话术:"},
                    ]

                    try:
                        sim_result = await structured_client.invoke_json(
                            model_config=request.simulator_model,
                            messages=sim_messages,
                            output_model=_SimOut,
                            stage="simulate_user",
                            temperature=0.7,
                        )
                        sim_out = sim_result.parsed
                        next_utterance = sim_out.utterance
                        should_continue = sim_out.should_continue
                    except Exception as exc:
                        next_utterance = "好的，谢谢。"
                        should_continue = False

                    if not next_utterance.strip():
                        should_continue = False
                        break

                    next_user_turn = TE(
                        id=timestamped_id("turn"),
                        run_id=run_id,
                        episode_id=episode_id,
                        turn_index=len(turns),
                        role=TurnRole.USER,
                        content=next_utterance,
                    )
                    turns.append(next_user_turn)
                    await _push({
                        "type": "turn",
                        "episode_id": episode_id,
                        "turn_id": next_user_turn.id,
                        "role": "user",
                        "content": next_utterance,
                        "turn_index": len(turns) - 1,
                    })

                episode = EpisodeExecution(
                    run_id=run_id,
                    episode_id=episode_id,
                    task_id=task_id,
                    scenario_id=scn.scenario_id,
                    turns=turns,
                    status=EpisodeStatus.COMPLETED,
                )
                all_episodes.append(episode)

                await _push({"type": "episode_end", "episode_id": episode_id, "turns_count": len(turns)})

                # Semantic Judge
                await _push({"type": "stage", "stage": "judging", "episode_id": episode_id, "message": "正在评分..."})
                judge = SemanticJudge(client=structured_client)
                try:
                    judge_result = await judge.evaluate_understanding(
                        understanding=understanding,
                        llm_scenario=scn,
                        episode=episode,
                        model_config=request.judge_model,
                    )
                    all_judge_results.append(judge_result)
                    await _push({
                        "type": "judge_result",
                        "episode_id": episode_id,
                        "scenario_id": scn.scenario_id,
                        "total_score": judge_result.total_score,
                        "overall_summary": judge_result.overall_summary,
                        "item_results": [r.model_dump() for r in judge_result.item_results],
                        "critical_failures": judge_result.critical_failures,
                    })
                except Exception as exc:
                    await _push({"type": "judge_error", "episode_id": episode_id, "error": str(exc)})

            # Final report
            avg_score = (
                sum(r.total_score for r in all_judge_results) / len(all_judge_results)
                if all_judge_results else 0.0
            )
            report_payload = {
                "run_id": run_id,
                "task_name": understanding.task_spec.get("task_name", ""),
                "instruction": request.instruction,
                "total_scenarios": len(scenarios_raw),
                "avg_score": round(avg_score, 1),
                "judge_results": [r.model_dump() for r in all_judge_results],
                "judge_plan": understanding.judge_plan.model_dump(mode="json"),
                "knowledge_facts": [kf.model_dump() for kf in understanding.knowledge_facts],
                "episodes": [
                    {
                        "episode_id": ep.episode_id,
                        "scenario_id": ep.scenario_id,
                        "turns": [
                            {"id": t.id, "role": str(t.role), "content": t.content}
                            for t in ep.turns
                        ],
                    }
                    for ep in all_episodes
                ],
            }
            repo.upsert_json("report_artifacts", run_id, report_payload)
            await _push({"type": "completed", "run_id": run_id, "avg_score": avg_score})

        except Exception as exc:
            await _push({"type": "error", "error": str(exc)})

    asyncio.create_task(_run())
    return {"ok": True, "run_id": run_id}


@app.get("/api/run/{run_id}/events")
async def run_events(run_id: str):
    """SSE 实时推送 run 事件。"""
    async def _generator() -> AsyncGenerator[str, None]:
        last_idx = 0
        timeout_iters = 0
        while timeout_iters < 600:  # max 10 minutes
            events = _run_events.get(run_id, [])
            while last_idx < len(events):
                ev = events[last_idx]
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                last_idx += 1
                if ev.get("type") in ("completed", "error"):
                    return
            await asyncio.sleep(1)
            timeout_iters += 1
        yield f"data: {json.dumps({'type': 'timeout'})}\n\n"

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/run/{run_id}/result")
async def run_result(run_id: str):
    """获取 run 结果（非 SSE，轮询或一次性查询）。"""
    events = _run_events.get(run_id, [])
    completed = next((e for e in reversed(events) if e.get("type") == "completed"), None)
    payload = repo.get_json("report_artifacts", run_id)
    return {
        "ok": bool(completed),
        "run_id": run_id,
        "events_count": len(events),
        "report": payload,
    }


@app.post("/api/conversation/import")
async def import_conversation(request: ImportConversationRequest):
    """导入已有对话 JSON，返回 run_id 和可再评分的 episode。"""
    run_id = request.run_id or f"import_{uuid.uuid4().hex[:8]}"
    from outbound_eval.domain.ids import timestamped_id
    from outbound_eval.domain.enums import EpisodeStatus, TurnRole

    episode_id = timestamped_id("ep")
    turns = []
    for i, t in enumerate(request.turns):
        role_str = str(t.get("role", "user")).lower()
        role_map = {"user": TurnRole.USER, "assistant": TurnRole.ASSISTANT, "system": TurnRole.SYSTEM}
        turns.append(TurnEvent(
            id=t.get("id", timestamped_id("turn")),
            run_id=run_id,
            episode_id=episode_id,
            turn_index=i,
            role=role_map.get(role_str, TurnRole.USER),
            content=t.get("content", ""),
        ))

    episode = EpisodeExecution(
        run_id=run_id,
        episode_id=episode_id,
        task_id=request.task_spec.get("task_id", "imported") if request.task_spec else "imported",
        scenario_id=request.scenario.get("scenario_id", "imported_scn"),
        turns=turns,
        status=EpisodeStatus.COMPLETED,
    )
    repo.upsert_json("imported_episodes", run_id, {
        "episode": episode.model_dump(mode="json"),
        "scenario": request.scenario,
        "judge_plan": request.judge_plan,
        "task_spec": request.task_spec,
    })
    return {"ok": True, "run_id": run_id, "episode_id": episode_id, "turns_count": len(turns)}


@app.post("/api/conversation/rejudge-imported")
async def rejudge_imported(request: RejudgeImportedRequest):
    """对导入的对话重新评分。"""
    data = repo.get_json("imported_episodes", request.run_id)
    if not data:
        raise HTTPException(404, "Imported episode not found")
    from outbound_eval.domain.schemas_understanding import (
        TaskUnderstanding as TaskUnd2, JudgePlan, RiskPlan
    )
    from outbound_eval.evaluator.semantic_judge import SemanticJudge
    from outbound_eval.llm.structured_client import StructuredLLMClient

    episode = EpisodeExecution.model_validate(data["episode"])
    scn_raw = data.get("scenario", {})
    judge_plan_raw = data.get("judge_plan")
    task_spec_raw = data.get("task_spec", {})

    if not judge_plan_raw:
        raise HTTPException(400, "No judge_plan in imported data")

    judge_plan = JudgePlan.model_validate(judge_plan_raw)
    understanding = TaskUnd2(
        task_spec=task_spec_raw or {},
        judge_plan=judge_plan,
        risk_plan={"task_id": "imported", "detected_risks": [], "coverage_requirements": []},
    )
    scn = LLMScenarioSpec(
        scenario_id=scn_raw.get("scenario_id", "imported_scn"),
        task_id=task_spec_raw.get("task_id", "imported") if task_spec_raw else "imported",
        title=scn_raw.get("title", "导入场景"),
        user_goal=scn_raw.get("user_goal", ""),
        hidden_user_goal=scn_raw.get("hidden_user_goal", ""),
        initial_user_utterance=scn_raw.get("initial_user_utterance", ""),
    )
    client = StructuredLLMClient()
    judge = SemanticJudge(client=client)
    result = await judge.evaluate_understanding(
        understanding=understanding,
        llm_scenario=scn,
        episode=episode,
        model_config=request.judge_model,
    )
    return {"ok": True, "judge_result": result.model_dump(mode="json")}


@app.get("/api/conversation/template")
async def conversation_template():
    """返回对话 JSON 导入模板。"""
    return {
        "description": "OutboundEval 对话导入模板，填写后通过 /api/conversation/import 导入",
        "template": {
            "run_id": "可选，留空自动生成",
            "scenario": {
                "scenario_id": "scn_001",
                "title": "场景标题",
                "user_goal": "用户表面目标描述",
                "hidden_user_goal": "隐藏测试目的（评分用）",
                "initial_user_utterance": "第一句用户话术",
                "linked_judge_point_ids": ["jp.001", "jp.002"],
                "covered_requirement_ids": ["req.001"],
            },
            "turns": [
                {"role": "user", "content": "您好，请问..."},
                {"role": "assistant", "content": "您好，我是..."},
                {"role": "user", "content": "我想了解..."},
            ],
            "judge_plan": {
                "task_id": "task_xxx",
                "judge_points": [
                    {
                        "id": "jp.001",
                        "dimension": "task_completion",
                        "criterion": "是否完成任务目标",
                        "pass_criteria": "清晰完成了任务目标",
                        "fail_criteria": "未完成任务目标",
                        "severity": "major",
                        "weight": 1.0,
                        "source_node_id": "",
                        "source_text": "",
                        "linked_requirement_ids": [],
                        "evaluator": "llm",
                    }
                ],
                "dimension_weights": {
                    "task_completion": 0.3,
                    "flow_following": 0.2,
                    "knowledge_correctness": 0.2,
                    "constraint_following": 0.15,
                    "exception_handling": 0.1,
                    "user_experience": 0.05,
                },
            },
            "task_spec": {
                "task_id": "task_xxx",
                "task_name": "任务名称",
                "role": "外呼客服",
                "objective": "任务目标",
            },
        },
    }


@app.get("/api/run/{run_id}/export")
async def export_run(run_id: str):
    """导出 run 的完整数据为 JSON（对话+评分+报告）。"""
    payload = repo.get_json("report_artifacts", run_id)
    if not payload:
        raise HTTPException(404, "Run not found")
    import io
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return StreamingResponse(
        io.StringIO(content),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="eval_run_{run_id}.json"'},
    )


# ===== 旧版 API 兼容 =====


@app.post("/api/compile")
async def compile_task(request: CompileRequest):
    result = InstructionCompileService().compile(request.instruction)
    if result.task_spec:
        repo.upsert_json("task_specs", result.task_spec.task_id, result.task_spec.model_dump(mode="json"))
    return result


@app.post("/api/qa")
async def qa_task(request: QARequest):
    task_spec = TaskSpec.model_validate(request.task_spec)
    return await SpecQAService().audit(request.instruction, task_spec)


@app.post("/api/plan")
async def plan_task(request: PlanRequest):
    task_spec = TaskSpec.model_validate(request.task_spec)
    matrix = CoveragePlanner().plan(task_spec, budget=request.budget)
    for scenario in matrix.scenarios:
        repo.upsert_json("scenario_definitions", scenario.scenario_id, scenario.model_dump(mode="json"))
    return matrix


@app.post("/api/run")
async def run_eval(request: RunRequest):
    connection = await OpenAICompatibleAdapter().test_connection(request.target_model_config)
    if not connection.ok:
        return {"ok": False, "stage": "connection_test", "connection": connection}
    model_config = request.target_model_config.model_copy(update={"connection_tested": True})
    compile_result = InstructionCompileService().compile(request.instruction)
    if not compile_result.task_spec:
        return {"ok": False, "stage": "compile", "compile_result": compile_result}
    task_spec = compile_result.task_spec
    qa = await SpecQAService().audit(request.instruction, task_spec)
    if not qa.passed:
        return {"ok": False, "stage": "qa", "qa": qa}
    coverage = CoveragePlanner().plan(task_spec, request.budget)
    if isinstance(repo, PostgresRepository):
        repo.init_db()
    runner = BatchRunner()
    runner.episode_runner.trace_store = PostgresTraceStore(settings().pg_dsn)
    out = Path("runs") / "web_latest"
    runner.episode_runner.audit_payload_dir = out
    runner.episode_runner.simulator_model_config = model_config
    run_result = await runner.run_matrix(task_spec, coverage.scenarios, [model_config], request.attempts, request.parallel)
    episodes = [item.episode for item in run_result.episode_results]
    judges = [judge for item in run_result.episode_results for judge in item.judges]
    score = ScoreAggregator().aggregate(task_spec, judges, run_id=run_result.run_id)
    report = ReportGenerator().build(task_spec, coverage, episodes, judges, score, model_config.redacted())
    paths = ReportGenerator().write(report, out)
    repo.upsert_json("evaluation_runs", run_result.run_id, run_result.model_dump(mode="json"))
    repo.upsert_json("report_artifacts", run_result.run_id, report.model_dump(mode="json"))
    badcases = []
    scenario_by_id = {scenario.scenario_id: scenario for scenario in coverage.scenarios}
    for result in run_result.episode_results:
        badcases.extend(BadcaseLibrary().from_judges(task_spec, scenario_by_id[result.episode.scenario_id], result.judges))
    for item in badcases:
        repo.upsert_json("badcase_items", item.id, item.model_dump(mode="json"))
    redis_state.set_run_status(run_result.run_id, {"stage": "completed", "status": "completed", "score": score.normalized_score})
    return {
        "ok": True,
        "run_id": run_result.run_id,
        "score": score.normalized_score,
        "report_html": str(paths["html"]),
        "report_url": f"/api/report/{run_result.run_id}/html",
        "coverage": coverage,
        "episodes": len(episodes),
        "judges": len(judges),
    }


@app.post("/api/rejudge")
async def rejudge(request: RejudgeRequest):
    task_spec = TaskSpec.model_validate(request.task_spec)
    scenario = ScenarioSpec.model_validate(request.scenario)
    episode = EpisodeExecution.model_validate(request.episode)
    judges, score = await RejudgeService().rejudge(task_spec, scenario, episode)
    return {"judges": [j.model_dump(mode="json") for j in judges], "score": score.model_dump(mode="json")}


@app.get("/api/status")
async def status():
    return {
        "evaluation_runs": repo.list_json("evaluation_runs")[:20],
        "reports": repo.list_json("report_artifacts")[:20],
        "badcases": repo.list_json("badcase_items")[:20],
    }


@app.get("/api/report/{run_id}")
async def report(run_id: str):
    payload = repo.get_json("report_artifacts", run_id)
    if not payload:
        raise HTTPException(404, "report not found")
    return payload


@app.get("/api/report/{run_id}/html", response_class=HTMLResponse)
async def report_html(run_id: str):
    payload = repo.get_json("report_artifacts", run_id)
    if not payload:
        raise HTTPException(404, "report not found")
    from outbound_eval.domain.schemas_report import ReportArtifact

    artifact = ReportArtifact.model_validate(payload)
    return ReportGenerator().render_html(artifact)


@app.post("/api/golden/seed")
async def golden_seed(payload: dict[str, Any]):
    task_id = payload.get("task_id", "task_unknown")
    scenario_ids = payload.get("scenario_ids", [])
    requirement_ids = payload.get("requirement_ids", [])
    cases, labels = GoldenSetService().sample_cases(task_id, scenario_ids, requirement_ids)
    for case in cases:
        repo.upsert_json("golden_cases", case.id, case.model_dump(mode="json"))
    for label in labels:
        repo.upsert_json("golden_labels", label.id, label.model_dump(mode="json"))
    return {"cases": cases, "labels": labels}


def main() -> None:
    uvicorn.run("outbound_eval.web.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
