from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.adapters import OpenAICompatibleAdapter
from outbound_eval.badcase import BadcaseLibrary
from outbound_eval.compiler import InstructionCompileService
from outbound_eval.config import settings
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.golden import GoldenSetService
from outbound_eval.planner import CoveragePlanner
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


class CompileRequest(BaseModel):
    instruction: str


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


@app.post("/api/model/test")
async def model_test(config: ModelConfig):
    return await OpenAICompatibleAdapter().test_connection(config)


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
