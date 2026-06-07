from __future__ import annotations

import asyncio
import json
from pathlib import Path

from outbound_eval.badcase import BadcaseLibrary
from outbound_eval.compiler import InstructionCompileService
from outbound_eval.domain.schemas_episode import ModelTurn
from outbound_eval.domain.schemas_model import ModelConfig, SessionHandle
from outbound_eval.planner import CoveragePlanner
from outbound_eval.reporting import ReportGenerator
from outbound_eval.runner import BatchRunner
from outbound_eval.scoring import ScoreAggregator


class LocalTargetAdapter:
    async def start_session(self, task_spec, variables, model_config):
        return SessionHandle(session_id="local_safe_session", task_id=task_spec.task_id, variables=variables)

    async def send_turn(self, session, messages, metadata):
        user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        if "开车" in user_text:
            text = "您在开车先注意安全，我这边不继续打扰，稍后再联系。"
        elif "奖励" in user_text or "优惠" in user_text or "费用" in user_text:
            text = "我不能承诺额外奖励、优惠或费用政策，只能按本次通知口径说明。"
        elif "拒绝" in user_text or "不想" in user_text:
            text = "理解，我先记录您不方便配送的原因，不会强迫您确认。"
        else:
            text = "您好，我是美团外卖客服。飞毛腿合同将生效，请确认是否可以按 X/Y/Z/W 规则配送。"
        return ModelTurn(content=text, finish_reason="stop", latency_ms=1)

    async def close_session(self, session):
        return None


async def main() -> None:
    out_dir = Path("runs/delivery_local")
    if out_dir.exists():
        for path in out_dir.glob("*"):
            if path.is_file():
                path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = Path("samples/rider_contract.md").read_text(encoding="utf-8")
    task_spec = InstructionCompileService().compile(raw).task_spec
    coverage = CoveragePlanner().plan(task_spec, budget=8)
    cfg = ModelConfig(base_url="local://safe-target", api_key="local-only", model_name="local-safe-target", connection_tested=True)

    runner = BatchRunner()
    runner.episode_runner.adapter = LocalTargetAdapter()
    runner.episode_runner.audit_payload_dir = out_dir
    run_result = await runner.run_matrix(task_spec, coverage.scenarios, [cfg], attempts=1, parallel=1)

    episodes = [result.episode for result in run_result.episode_results]
    judges = [judge for result in run_result.episode_results for judge in result.judges]
    score = ScoreAggregator().aggregate(task_spec, judges, run_result.run_id)
    report = ReportGenerator().build(task_spec, coverage, episodes, judges, score, cfg.redacted())
    ReportGenerator().write(report, out_dir)

    (out_dir / "coverage_matrix.json").write_text(coverage.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "episodes.jsonl").write_text(
        "\n".join(json.dumps(ep.model_dump(mode="json"), ensure_ascii=False) for ep in episodes) + "\n",
        encoding="utf-8",
    )
    (out_dir / "judge_events.jsonl").write_text(
        "\n".join(json.dumps(judge.model_dump(mode="json"), ensure_ascii=False) for judge in judges) + "\n",
        encoding="utf-8",
    )

    scenario_by_id = {scenario.scenario_id: scenario for scenario in coverage.scenarios}
    badcases = []
    for result in run_result.episode_results:
        badcases.extend(BadcaseLibrary().from_judges(task_spec, scenario_by_id[result.episode.scenario_id], result.judges))
    (out_dir / "badcases.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in badcases], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "run_id": run_result.run_id,
                "out": str(out_dir),
                "episodes": len(episodes),
                "judges": len(judges),
                "score": score.normalized_score,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

