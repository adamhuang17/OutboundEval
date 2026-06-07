from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from outbound_eval.adapters.openai_compatible import OpenAICompatibleAdapter
from outbound_eval.badcase import BadcaseLibrary
from outbound_eval.compiler import InstructionCompileService
from outbound_eval.config import settings
from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_report import ReportArtifact
from outbound_eval.domain.schemas_scenario import CoverageMatrix, ScenarioSpec
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.planner import CoveragePlanner
from outbound_eval.reporting import ReportGenerator
from outbound_eval.runner import BatchRunner
from outbound_eval.runner.rejudge import RejudgeService
from outbound_eval.scoring import ScoreAggregator
from outbound_eval.spec_qa import SpecQAService
from outbound_eval.status import RedisStateStore
from outbound_eval.storage import PostgresRepository, SQLiteRepository, default_repository
from outbound_eval.trace import PostgresTraceStore, SQLiteTraceStore, default_trace_store


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="outbound-eval")
    sub = parser.add_subparsers(required=True)

    init_db = sub.add_parser("init-db")
    init_db.add_argument("--pg-dsn", default=settings().pg_dsn)
    init_db.add_argument("--redis-url", default=settings().redis_url)
    init_db.set_defaults(func=cmd_init_db)

    model = sub.add_parser("model")
    model_sub = model.add_subparsers(required=True)
    model_test = model_sub.add_parser("test")
    add_model_args(model_test)
    model_test.set_defaults(func=cmd_model_test)

    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument("--input", required=True)
    compile_cmd.add_argument("--out", default="runs/compiled")
    add_model_args(compile_cmd)
    compile_cmd.set_defaults(func=cmd_compile)

    qa_cmd = sub.add_parser("qa")
    qa_cmd.add_argument("--task-spec", required=True)
    qa_cmd.add_argument("--raw", required=True)
    qa_cmd.set_defaults(func=cmd_qa)

    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--task-spec", required=True)
    plan_cmd.add_argument("--budget", type=int, default=12)
    plan_cmd.add_argument("--out", default="runs/planned")
    plan_cmd.set_defaults(func=cmd_plan)

    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--instruction", required=True)
    add_model_args(run_cmd)
    run_cmd.add_argument("--budget", type=int, default=12)
    run_cmd.add_argument("--attempts", type=int, default=1)
    run_cmd.add_argument("--parallel", type=int, default=1)
    run_cmd.add_argument("--fresh", action="store_true")
    run_cmd.add_argument("--retry-failures", action="store_true")
    run_cmd.add_argument("--out-dir", default="runs/latest")
    run_cmd.add_argument("--pg-dsn", default=settings().pg_dsn)
    run_cmd.add_argument("--redis-url", default=settings().redis_url)
    run_cmd.add_argument("--sqlite-db", default=None, help="Testing fallback only; formal delivery uses PostgreSQL.")
    run_cmd.set_defaults(func=cmd_run)

    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--pg-dsn", default=settings().pg_dsn)
    status_cmd.add_argument("--redis-url", default=settings().redis_url)
    status_cmd.set_defaults(func=cmd_status)

    rejudge_cmd = sub.add_parser("rejudge")
    rejudge_cmd.add_argument("--task-spec", required=True)
    rejudge_cmd.add_argument("--scenario", required=True)
    rejudge_cmd.add_argument("--episode", required=True)
    rejudge_cmd.add_argument("--out", default="runs/rejudge")
    rejudge_cmd.set_defaults(func=cmd_rejudge)

    report_cmd = sub.add_parser("report")
    report_cmd.add_argument("--report-json", required=True)
    report_cmd.set_defaults(func=cmd_report)

    badcase = sub.add_parser("badcase")
    badcase_sub = badcase.add_subparsers(required=True)
    badcase_list = badcase_sub.add_parser("list")
    badcase_list.add_argument("--pg-dsn", default=settings().pg_dsn)
    badcase_list.set_defaults(func=cmd_badcase_list)
    badcase_replay = badcase_sub.add_parser("replay")
    badcase_replay.add_argument("--badcase-json", required=True)
    badcase_replay.set_defaults(func=cmd_badcase_replay)

    return parser


def cmd_init_db(args: argparse.Namespace) -> int:
    repo = PostgresRepository(args.pg_dsn)
    repo.init_db()
    redis_state = RedisStateStore(args.redis_url)
    redis_ok = redis_state.ping()
    print(json.dumps({"postgres": "ok", "redis": "ok" if redis_ok else "failed"}, ensure_ascii=False, indent=2))
    return 0


def add_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-seconds", type=int, default=30)


def model_config_from_args(args: argparse.Namespace, tested: bool = False) -> ModelConfig:
    return ModelConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        model_name=args.model_name,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
        connection_tested=tested,
    )


def cmd_model_test(args: argparse.Namespace) -> int:
    config = model_config_from_args(args)
    result = asyncio.run(OpenAICompatibleAdapter().test_connection(config))
    print(result.model_dump_json(indent=2))
    return 0 if result.ok else 2


def cmd_compile(args: argparse.Namespace) -> int:
    raw = Path(args.input).read_text(encoding="utf-8")
    result = InstructionCompileService().compile(raw, model_config=model_config_from_args(args))
    InstructionCompileService().write_outputs(result, Path(args.out))
    print(f"compile status={result.status} out={args.out}")
    if result.compile_error:
        print(result.compile_error.message)
    return 0 if result.status == "ok" else 2


def cmd_qa(args: argparse.Namespace) -> int:
    task_spec = TaskSpec.model_validate_json(Path(args.task_spec).read_text(encoding="utf-8"))
    raw = Path(args.raw).read_text(encoding="utf-8")
    result = asyncio.run(SpecQAService().audit(raw, task_spec))
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


def cmd_plan(args: argparse.Namespace) -> int:
    task_spec = TaskSpec.model_validate_json(Path(args.task_spec).read_text(encoding="utf-8"))
    matrix = CoveragePlanner().plan(task_spec, budget=args.budget)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "coverage_matrix.json").write_text(matrix.model_dump_json(indent=2), encoding="utf-8")
    for scenario in matrix.scenarios:
        (out / f"{scenario.scenario_id}.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
    print(f"planned scenarios={len(matrix.scenarios)} coverage={matrix.requirement_coverage_rate:.2%} out={out}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = Path(args.instruction).read_text(encoding="utf-8")
    config = model_config_from_args(args)
    redis_state = RedisStateStore(args.redis_url)
    fingerprint = _model_fingerprint(config)
    cached = redis_state.get_cached_connection_test(fingerprint)
    if cached and cached.get("ok"):
        connection = cached
    else:
        connection_result = await OpenAICompatibleAdapter().test_connection(config)
        connection = connection_result.model_dump(mode="json")
        redis_state.cache_connection_test(fingerprint, connection)
    (out / "connection_test.json").write_text(json.dumps(connection, ensure_ascii=False, indent=2), encoding="utf-8")
    if not connection.get("ok"):
        print(f"connection failed type={connection.get('error_type')} base_url={connection.get('base_url')} model={connection.get('model_name')}")
        print(connection.get("error_message"))
        return 2
    config = model_config_from_args(args, tested=True)
    compile_result = InstructionCompileService().compile(raw, model_config=config)
    InstructionCompileService().write_outputs(compile_result, out)
    if not compile_result.task_spec:
        print(f"compile failed: {compile_result.compile_error.message if compile_result.compile_error else 'unknown'}")
        return 2
    task_spec = compile_result.task_spec
    qa = await SpecQAService().audit(raw, task_spec)
    (out / "qa_result.json").write_text(qa.model_dump_json(indent=2), encoding="utf-8")
    if not qa.passed:
        print(f"qa blocked critical findings={len(qa.blocking_findings)}")
        return 2
    coverage = CoveragePlanner().plan(task_spec, budget=args.budget)
    (out / "coverage_matrix.json").write_text(coverage.model_dump_json(indent=2), encoding="utf-8")
    repo = SQLiteRepository(args.sqlite_db) if args.sqlite_db else PostgresRepository(args.pg_dsn)
    if hasattr(repo, "init_db"):
        repo.init_db()
    trace = SQLiteTraceStore(args.sqlite_db) if args.sqlite_db else PostgresTraceStore(args.pg_dsn)
    runner = BatchRunner()
    runner.episode_runner.trace_store = trace
    runner.episode_runner.audit_payload_dir = out
    runner.episode_runner.simulator_model_config = config
    redis_state.set_run_status("pending", {"stage": "starting", "status": "running"})
    run_result = await runner.run_matrix(task_spec, coverage.scenarios, [config], attempts=args.attempts, parallel=args.parallel)
    redis_state.set_run_status(run_result.run_id, {"stage": "episodes_completed", "status": "running", "episodes": len(run_result.episode_results)})
    episodes = [item.episode for item in run_result.episode_results]
    judges = [judge for item in run_result.episode_results for judge in item.judges]
    score = ScoreAggregator().aggregate(task_spec, judges, run_id=run_result.run_id)
    report = ReportGenerator().build(task_spec, coverage, episodes, judges, score, config.redacted())
    paths = ReportGenerator().write(report, out)
    repo.upsert_json("evaluation_runs", run_result.run_id, run_result.model_dump(mode="json"))
    repo.upsert_json("report_artifacts", run_result.run_id, report.model_dump(mode="json"))
    for ep in episodes:
        repo.upsert_json("episode_executions", ep.episode_id, ep.model_dump(mode="json"))
        for turn in ep.turns:
            repo.upsert_json("turn_events", turn.id, turn.model_dump(mode="json"))
    for judge in judges:
        repo.upsert_json("judge_events", judge.id, judge.model_dump(mode="json"))
    for item in score.items:
        repo.upsert_json("score_items", item.id, item.model_dump(mode="json"))
    badcases = []
    scenario_by_id = {scenario.scenario_id: scenario for scenario in coverage.scenarios}
    for result in run_result.episode_results:
        scenario = scenario_by_id[result.episode.scenario_id]
        badcases.extend(BadcaseLibrary().from_judges(task_spec, scenario, result.judges))
    for item in badcases:
        repo.upsert_json("badcase_items", item.id, item.model_dump(mode="json"))
    _write_jsonl(out / "episodes.jsonl", [ep.model_dump(mode="json") for ep in episodes])
    _write_jsonl(out / "judge_events.jsonl", [judge.model_dump(mode="json") for judge in judges])
    (out / "badcases.json").write_text(json.dumps([item.model_dump(mode="json") for item in badcases], ensure_ascii=False, indent=2), encoding="utf-8")
    redis_state.set_run_status(run_result.run_id, {"stage": "completed", "status": "completed", "score": score.normalized_score, "report_html": str(paths["html"])})
    print(f"run_id={run_result.run_id} episodes={len(episodes)} score={score.normalized_score} report={paths['html']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = PostgresRepository(args.pg_dsn)
    runs = repo.list_json("evaluation_runs")
    redis_ok = RedisStateStore(args.redis_url).ping()
    print(json.dumps({"redis": redis_ok, "runs": len(runs), "latest": runs[:3]}, ensure_ascii=False, indent=2))
    return 0


def cmd_rejudge(args: argparse.Namespace) -> int:
    task_spec = TaskSpec.model_validate_json(Path(args.task_spec).read_text(encoding="utf-8"))
    scenario = ScenarioSpec.model_validate_json(Path(args.scenario).read_text(encoding="utf-8"))
    episode = EpisodeExecution.model_validate_json(Path(args.episode).read_text(encoding="utf-8"))
    judges, score = asyncio.run(RejudgeService().rejudge(task_spec, scenario, episode))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "judges.json").write_text(json.dumps([j.model_dump(mode="json") for j in judges], ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "score.json").write_text(score.model_dump_json(indent=2), encoding="utf-8")
    print(f"rejudge episode_id={episode.episode_id} judges={len(judges)} score={score.normalized_score}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    artifact = ReportArtifact.model_validate_json(Path(args.report_json).read_text(encoding="utf-8"))
    print(ReportGenerator().render_markdown(artifact))
    return 0


def cmd_badcase_list(args: argparse.Namespace) -> int:
    repo = PostgresRepository(args.pg_dsn)
    print(json.dumps(repo.list_json("badcase_items"), ensure_ascii=False, indent=2))
    return 0


def cmd_badcase_replay(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.badcase_json).read_text(encoding="utf-8"))
    print(json.dumps({"replay_config": payload.get("replay_config"), "rejudge_only": True}, ensure_ascii=False, indent=2))
    return 0


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _model_fingerprint(config: ModelConfig) -> str:
    raw = f"{config.base_url}|{config.model_name}|{hashlib.sha256(config.raw_api_key().encode()).hexdigest()[:12]}"
    return hashlib.sha256(raw.encode()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
