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
from outbound_eval.compiler.compile_qa import CompileQAGate
from outbound_eval.compiler.llm_task_compiler import LLMTaskCompiler
from outbound_eval.config import settings
from outbound_eval.domain.schemas_episode import EpisodeExecution, TurnEvent
from outbound_eval.domain.ids import timestamped_id
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
from outbound_eval.planner.scenario_planner_llm import ScenarioPlannerLLM
from outbound_eval.planner.scenario_builder_llm import ScenarioBuilderLLM
from outbound_eval.planner.scenario_qa import ScenarioQAGate
from outbound_eval.simulator.dialogue_manager import DialogueManager
from outbound_eval.evaluator.evidence_mapper import EvidenceMapper
from outbound_eval.evaluator.finding_aggregator import FindingAggregator
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

class FourModelConfigs(BaseModel):
    """前端必须同时配置四个 LLM 角色。"""
    compiler_model: ModelConfig
    target_model: ModelConfig
    simulator_model: ModelConfig
    judge_model: ModelConfig


class TestAllModelsRequest(BaseModel):
    configs: FourModelConfigs


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
    target_model: ModelConfig
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
async def test_all_models(request: TestAllModelsRequest):
    """同时测试四个 LLM 角色配置，全部通过才返回 ok=True。"""
    adapter = OpenAICompatibleAdapter()
    results = await asyncio.gather(
        adapter.test_connection(request.configs.compiler_model),
        adapter.test_connection(request.configs.target_model),
        adapter.test_connection(request.configs.simulator_model),
        adapter.test_connection(request.configs.judge_model),
        return_exceptions=True,
    )
    compiler_r = results[0] if not isinstance(results[0], Exception) else None
    target_r = results[1] if not isinstance(results[1], Exception) else None
    simulator_r = results[2] if not isinstance(results[2], Exception) else None
    judge_r = results[3] if not isinstance(results[3], Exception) else None

    def _fmt(r, label):
        if r is None:
            return {"role": label, "ok": False, "error": "exception"}
        return {"role": label, "ok": r.ok, "latency_ms": r.latency_ms, "error": r.error_message}

    all_ok = all(
        (r is not None and r.ok)
        for r in [compiler_r, target_r, simulator_r, judge_r]
    )
    return {
        "ok": all_ok,
        "details": [
            _fmt(compiler_r, "compiler"),
            _fmt(target_r, "target"),
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
        compile_qa = CompileQAGate().validate(understanding)
        repo.upsert_json(
            "task_understandings",
            understanding.task_spec.get("task_id", "unknown"),
            understanding.model_dump(mode="json"),
        )
        return {
            "ok": compile_qa.passed,
            "understanding": understanding.model_dump(mode="json"),
            "compile_qa": compile_qa.model_dump(mode="json"),
            "error": None if compile_qa.passed else "CompileQAGate blocked this task understanding.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/scenarios/build")
async def build_scenarios(request: BuildScenariosRequest):
    """LLM 生成测试场景。"""
    try:
        understanding = TaskUnderstanding.model_validate(request.understanding)
        compile_qa = CompileQAGate().validate(understanding)
        if not compile_qa.passed:
            return {
                "ok": False,
                "error": "CompileQAGate blocked scenario building.",
                "compile_qa": compile_qa.model_dump(mode="json"),
            }
        persona = EvaluatorPersonaInput.model_validate(request.persona) if request.persona else EvaluatorPersonaInput()
        planner = ScenarioPlannerLLM()
        plan = await planner.plan(
            understanding=understanding,
            persona=persona,
            scenario_count=request.scenario_count,
            model_config=request.llm_config,
        )
        builder = ScenarioBuilderLLM()
        scenario_set = await builder.build(
            understanding=understanding,
            persona=persona,
            scenario_count=request.scenario_count,
            model_config=request.llm_config,
            plan=plan,
        )
        scenario_qa = ScenarioQAGate().validate(understanding, scenario_set)
        return {
            "ok": scenario_qa.passed,
            "scenario_plan": plan.model_dump(mode="json"),
            "scenario_set": scenario_set.model_dump(mode="json"),
            "scenario_qa": scenario_qa.model_dump(mode="json"),
            "error": None if scenario_qa.passed else "ScenarioQAGate blocked this scenario set.",
        }
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
            understanding = TaskUnderstanding.model_validate(request.understanding)
            compile_qa = CompileQAGate().validate(understanding)
            if not compile_qa.passed:
                await _push({"type": "error", "error": "CompileQAGate blocked this run.", "compile_qa": compile_qa.model_dump(mode="json")})
                return

            scenarios = [LLMScenarioSpec.model_validate(item) for item in request.scenarios]
            scenario_set = ScenarioSet(task_id=understanding.task_spec.get("task_id", "task_unknown"), scenarios=scenarios)
            scenario_qa = ScenarioQAGate().validate(understanding, scenario_set)
            if not scenario_qa.passed:
                await _push({"type": "error", "error": "ScenarioQAGate blocked this run.", "scenario_qa": scenario_qa.model_dump(mode="json")})
                return

            await _push({"type": "stage", "stage": "run_ready", "message": f"场景数量: {len(scenarios)}"})

            from outbound_eval.evaluator.semantic_judge import SemanticJudge

            all_judge_results = []
            all_episodes = []
            all_target_payloads: list[dict[str, Any]] = []
            all_visibility_violations: list[str] = []
            dialogue = DialogueManager()
            judge = SemanticJudge()
            evidence_mapper = EvidenceMapper()

            for scn_idx, scn in enumerate(scenarios):
                episode_id = timestamped_id("ep")
                await _push({
                    "type": "episode_start",
                    "episode_id": episode_id,
                    "scenario_id": scn.scenario_id,
                    "scenario_title": scn.title,
                    "scn_index": scn_idx,
                    "total": len(scenarios),
                })

                dialogue_result = await dialogue.run_episode(
                    run_id=run_id,
                    understanding=understanding,
                    scenario=scn,
                    raw_instruction=request.instruction,
                    target_model_config=request.target_model,
                    simulator_model_config=request.simulator_model,
                    episode_id=episode_id,
                )
                episode = dialogue_result.episode
                all_target_payloads.extend(dialogue_result.target_payloads)
                all_visibility_violations.extend(dialogue_result.visibility_violations)
                all_episodes.append(episode)

                await _push({
                    "type": "episode_bound",
                    "episode_id": episode.episode_id,
                    "scenario_id": scn.scenario_id,
                    "status": str(episode.status),
                    "termination_reason": episode.termination_reason,
                })
                for turn in episode.turns:
                    await _push({
                        "type": "turn",
                        "episode_id": episode.episode_id,
                        "turn_id": turn.id,
                        "role": str(turn.role),
                        "content": turn.content,
                        "turn_index": turn.turn_index,
                    })
                await _push({"type": "episode_end", "episode_id": episode.episode_id, "turns_count": len(episode.turns)})

                # Semantic Judge
                await _push({"type": "stage", "stage": "judging", "episode_id": episode.episode_id, "message": "正在评分..."})
                try:
                    judge_result = await judge.evaluate_understanding(
                        understanding=understanding,
                        llm_scenario=scn,
                        episode=episode,
                        model_config=request.judge_model,
                    )
                    judge_result = evidence_mapper.map_semantic_result(episode, judge_result)
                    all_judge_results.append(judge_result)
                    await _push({
                        "type": "judge_result",
                        "episode_id": episode.episode_id,
                        "scenario_id": scn.scenario_id,
                        "total_score": judge_result.total_score,
                        "overall_summary": judge_result.overall_summary,
                        "item_results": [r.model_dump() for r in judge_result.item_results],
                        "critical_failures": judge_result.critical_failures,
                    })
                except Exception as exc:
                    await _push({"type": "judge_error", "episode_id": episode.episode_id, "error": str(exc)})

            # Final report
            avg_score = (
                sum(r.total_score for r in all_judge_results) / len(all_judge_results)
                if all_judge_results else 0.0
            )
            aggregated_findings = FindingAggregator().merge(semantic_results=all_judge_results)
            report_payload = {
                "run_id": run_id,
                "task_name": understanding.task_spec.get("task_name", ""),
                "instruction": request.instruction,
                "total_scenarios": len(scenarios),
                "avg_score": round(avg_score, 1),
                "judge_results": [r.model_dump() for r in all_judge_results],
                "judge_plan": understanding.judge_plan.model_dump(mode="json"),
                "scenario_qa": scenario_qa.model_dump(mode="json"),
                "knowledge_facts": [kf.model_dump() for kf in understanding.knowledge_facts],
                "source_map": {key: value.model_dump(mode="json") for key, value in understanding.source_map.items()},
                "target_payloads": all_target_payloads,
                "visibility_violations": all_visibility_violations,
                "findings": [finding.model_dump(mode="json") for finding in aggregated_findings],
                "episodes": [
                    {
                        "episode_id": ep.episode_id,
                        "scenario_id": ep.scenario_id,
                        "status": str(ep.status),
                        "termination_reason": ep.termination_reason,
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
    raise HTTPException(
        status_code=410,
        detail="Legacy /api/compile was removed from the default product path. Use /api/task/understand.",
    )


@app.post("/api/qa")
async def qa_task(request: QARequest):
    task_spec = TaskSpec.model_validate(request.task_spec)
    return await SpecQAService().audit(request.instruction, task_spec)


@app.post("/api/plan")
async def plan_task(request: PlanRequest):
    raise HTTPException(
        status_code=410,
        detail="Legacy /api/plan was removed from the default product path. Use /api/scenarios/build.",
    )


@app.post("/api/run")
async def run_eval(request: RunRequest):
    raise HTTPException(
        status_code=410,
        detail="Legacy /api/run was removed from the default product path. Use /api/run/start with TaskUnderstanding and ScenarioSet.",
    )


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
