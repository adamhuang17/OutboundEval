from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from outbound_eval.adapters.openai_compatible import OpenAICompatibleAdapter
from outbound_eval.badcase import BadcaseLibrary
from outbound_eval.compiler import InstructionCompileService
from outbound_eval.domain.enums import EpisodeStatus, TurnRole, Verdict
from outbound_eval.domain.schemas_episode import EpisodeExecution, ModelTurn, TurnEvent
from outbound_eval.domain.schemas_model import ModelConfig, SessionHandle
from outbound_eval.domain.schemas_scenario import ScenarioSpec
from outbound_eval.evaluator.knowledge_checker import KnowledgeChecker
from outbound_eval.evaluator.rule_checker import RuleChecker
from outbound_eval.planner import CoveragePlanner
from outbound_eval.reporting import ReportGenerator
from outbound_eval.runner import EpisodeRunner
from outbound_eval.runner.rejudge import RejudgeService
from outbound_eval.scoring import ScoreAggregator
from outbound_eval.simulator.user_simulator import LLMUserSimulator
from outbound_eval.spec_qa import SpecQAService


ROOT = Path(__file__).resolve().parents[1]
RIDER = (ROOT / "samples" / "rider_contract.md").read_text(encoding="utf-8")


class StubAdapter:
    async def start_session(self, task_spec, variables, model_config):
        return SessionHandle(session_id="sess_test", task_id=task_spec.task_id, variables=variables)

    async def send_turn(self, session, messages, metadata):
        joined = "\n".join(m["content"] for m in messages)
        if "开车" in joined:
            return ModelTurn(content="您在开车先注意安全，我这边不打扰您，稍后再联系。")
        if "奖励" in joined:
            return ModelTurn(content="不能保证奖励或优惠，我只能按通知口径说明。")
        return ModelTurn(content="您好，我是美团外卖客服。飞毛腿合同将生效，请确认是否可以按要求配送。")

    async def close_session(self, session):
        return None


class OutboundEvalAcceptanceTest(unittest.TestCase):
    def compile_task(self):
        result = InstructionCompileService().compile(RIDER)
        self.assertEqual(result.status, "ok", result.compile_error)
        self.assertIsNotNone(result.task_spec)
        return result.task_spec

    def test_instruction_compile_official_prompt_to_task_spec(self):
        task_spec = self.compile_task()
        self.assertTrue(task_spec.task_id.startswith("task_"))
        self.assertGreaterEqual(len(task_spec.requirements), 6)
        self.assertTrue(all(req.id.startswith("req.") for req in task_spec.requirements))
        self.assertTrue(task_spec.faq_facts)
        self.assertTrue(task_spec.rubric)

    def test_qa_gate_detects_missing_faq(self):
        task_spec = self.compile_task().model_copy(update={"faq_facts": []})
        result = asyncio.run(SpecQAService().audit(RIDER, task_spec))
        self.assertTrue(any(f.requirement_ref == "faq_facts" for f in result.findings))

    def test_coverage_budget_12_covers_key_scenarios(self):
        task_spec = self.compile_task()
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        self.assertEqual(len(matrix.scenarios), 12)
        types = {str(s.scenario_type) for s in matrix.scenarios}
        self.assertIn("exception", types)
        self.assertIn("faq_probe", types)
        self.assertIn("constraint_risk", types)
        self.assertFalse(matrix.uncovered_requirement_ids)

    def test_simulator_does_not_leak_hidden_goal_to_target_context(self):
        task_spec = self.compile_task()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        messages = LLMUserSimulator().target_visible_context(task_spec.source_text, {}, [])
        combined = "\n".join(m["content"] for m in messages)
        self.assertNotIn(scenario.hidden_goal, combined)
        self.assertNotIn("coverage matrix", combined.lower())

    def test_episode_runner_max_10_and_failure_persists_trace_shape(self):
        task_spec = self.compile_task()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0].model_copy(update={"max_turns": 3})
        config = ModelConfig(base_url="http://example.test/v1", api_key="secret-key", model_name="stub", connection_tested=True)
        result = asyncio.run(EpisodeRunner(adapter=StubAdapter()).run_episode(task_spec, scenario, config, run_id="run_test"))
        self.assertIn(result.episode.status, {EpisodeStatus.COMPLETED, "completed"})
        self.assertLessEqual(len([t for t in result.episode.turns if t.role == "user"]), 3)
        self.assertIsNotNone(result.score)

    def test_rule_checker_opening_and_forbidden_reward(self):
        task_spec = self.compile_task()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        ep = EpisodeExecution(run_id="run_test", episode_id="ep_test", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_test", turn_index=1, role=TurnRole.ASSISTANT, content="您好，我是美团外卖客服。我保证奖励。"))
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        self.assertTrue(any(j.verdict == Verdict.FAIL and j.severity == "critical" for j in judges))

    def test_knowledge_checker_distinguishes_correct_and_wrong_faq(self):
        task_spec = self.compile_task()
        faq_req = task_spec.faq_facts[0].requirement_ids[0]
        scenario = CoveragePlanner().plan(task_spec).scenarios[0].model_copy(update={"covered_requirement_ids": [faq_req]})
        correct = EpisodeExecution(run_id="run_test", episode_id="ep_ok", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        correct.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_ok", turn_index=1, role=TurnRole.ASSISTANT, content="以 X/Y/Z/W 规则为准，不得自行编造订单数。"))
        wrong = EpisodeExecution(run_id="run_test", episode_id="ep_bad", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        wrong.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_bad", turn_index=1, role=TurnRole.ASSISTANT, content="肯定一天 99 单。"))
        ok_judge = asyncio.run(KnowledgeChecker().evaluate(task_spec, scenario, correct))[0]
        bad_judge = asyncio.run(KnowledgeChecker().evaluate(task_spec, scenario, wrong))[0]
        self.assertEqual(ok_judge.verdict, Verdict.PASS)
        self.assertEqual(bad_judge.verdict, Verdict.FAIL)

    def test_severity_guard_caps_fabricated_reward(self):
        task_spec = self.compile_task()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        ep = EpisodeExecution(run_id="run_test", episode_id="ep_cap", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_cap", turn_index=1, role=TurnRole.ASSISTANT, content="我保证奖励。"))
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        score = ScoreAggregator().aggregate(task_spec, judges, "run_test", "ep_cap")
        self.assertLessEqual(score.normalized_score, 60.0)

    def test_report_failed_item_links_turn_evidence(self):
        task_spec = self.compile_task()
        matrix = CoveragePlanner().plan(task_spec)
        scenario = matrix.scenarios[0]
        ep = EpisodeExecution(run_id="run_test", episode_id="ep_report", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_report", turn_index=1, role=TurnRole.ASSISTANT, content="我保证奖励。"))
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        score = ScoreAggregator().aggregate(task_spec, judges, "run_test")
        report = ReportGenerator().build(task_spec, matrix, [ep], judges, score, {"model_name": "stub"})
        self.assertTrue(report.failed_items)
        self.assertIn("t1", report.evidence_index)

    def test_rejudge_does_not_call_target_model(self):
        task_spec = self.compile_task()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        ep = EpisodeExecution(run_id="run_test", episode_id="ep_rejudge", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_test", episode_id="ep_rejudge", turn_index=1, role=TurnRole.ASSISTANT, content="我保证奖励。"))
        judges, score = asyncio.run(RejudgeService().rejudge(task_spec, scenario, ep))
        self.assertTrue(judges)
        self.assertIsNotNone(score)


if __name__ == "__main__":
    unittest.main()

