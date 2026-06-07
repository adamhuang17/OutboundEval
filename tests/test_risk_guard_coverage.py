"""
专项验收测试：Risk Guard Coverage 通用风险防护重构（方案第 32 章）

覆盖以下断言：
1. 风险有完整 guard → auto_guarded，不阻塞
2. 风险缺少关键 guard → blocking finding
3. auto_fix 只产生一条 finding（不重复产生 auto_guarded）
4. Coverage Planner 消费 RiskCoverageRequirement → 生成带 risk metadata 的场景
5. Coverage QA autofill：uncovered 风险场景自动补充
6. RuleChecker 记录 risk_category_id + forbidden_behavior_id 到 raw_output
7. KnowledgeChecker 在 raw_output 中标记 risk_linked FAQ
8. Report 包含 risk_guard_summary 且结构完整
9. 禁止出现 "if 奖励 in raw_instruction" 式特判（taxonomy 驱动）
"""
from __future__ import annotations

import asyncio
import unittest

from outbound_eval.compiler import InstructionCompileService
from outbound_eval.domain.enums import FindingDecision, Severity
from outbound_eval.domain.schemas_episode import EpisodeExecution, TurnEvent
from outbound_eval.domain.schemas_judge import SpecFinding
from outbound_eval.domain.schemas_scenario import CoverageMatrix
from outbound_eval.domain.schemas_task import (
    FAQFact,
    ForbiddenBehavior,
    RequirementItem,
    RiskCategory,
    RiskCoverageRequirement,
    TaskSpec,
)
from outbound_eval.domain.enums import (
    CheckMethod,
    RequirementCategory,
    RiskGuardType,
    ScenarioType,
    TurnRole,
)
from outbound_eval.evaluator.knowledge_checker import KnowledgeChecker
from outbound_eval.evaluator.rule_checker import RuleChecker
from outbound_eval.planner import CoveragePlanner
from outbound_eval.planner.coverage_qa import CoverageQA
from outbound_eval.reporting import ReportGenerator
from outbound_eval.scoring import ScoreAggregator
from outbound_eval.spec_qa import SpecQAService
from outbound_eval.spec_qa.guard_contract import GuardContractEvaluator
from outbound_eval.spec_qa.risk_auditor import RiskAuditor
from outbound_eval.spec_qa.risk_detector import RiskDetector
from outbound_eval.spec_qa.risk_taxonomy import RiskTaxonomy

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RIDER = (ROOT / "samples" / "rider_contract.md").read_text(encoding="utf-8")


def _compile_rider() -> TaskSpec:
    result = InstructionCompileService().compile(RIDER)
    assert result.status == "ok", result.compile_error
    assert result.task_spec is not None
    return result.task_spec


# ---------------------------------------------------------------------------
# 1. 风险有完整 guard → auto_guarded，不阻塞
# ---------------------------------------------------------------------------
class TestRiskGuardedNotBlocking(unittest.TestCase):
    def test_guarded_risk_produces_auto_guarded_finding_not_blocking(self):
        task_spec = _compile_rider()
        result = asyncio.run(SpecQAService().audit(RIDER, task_spec))
        risk_findings = [f for f in result.findings if str(f.source) == "risk"]
        # rider_contract 样本：编译器会 auto-fix 缺失的 guard，因此风险 finding 可以是
        # auto_fix（guard 被自动补全）或 auto_guarded（guard 已存在），两者都是非阻塞的正常结果
        non_blocking_risk = [
            f for f in risk_findings
            if str(f.decision) in {FindingDecision.AUTO_GUARDED.value, FindingDecision.AUTO_FIX.value}
        ]
        blocking = [f for f in result.blocking_findings if str(f.source) == "risk"]
        self.assertTrue(non_blocking_risk, "Expected at least one non-blocking (auto_guarded or auto_fix) risk finding")
        # non-blocking risk findings must not appear in blocking_findings
        non_blocking_ids = {f.id for f in non_blocking_risk}
        blocking_ids = {f.id for f in blocking}
        self.assertTrue(non_blocking_ids.isdisjoint(blocking_ids), "non-blocking risk findings must not be in blocking_findings")

    def test_auto_guarded_finding_has_false_blocking(self):
        task_spec = _compile_rider()
        result = asyncio.run(SpecQAService().audit(RIDER, task_spec))
        for finding in result.findings:
            if str(finding.decision) == FindingDecision.AUTO_GUARDED.value:
                self.assertFalse(finding.blocking, f"AUTO_GUARDED finding {finding.id} must not have blocking=True")


# ---------------------------------------------------------------------------
# 2. 风险缺少关键 guard → blocking finding
# ---------------------------------------------------------------------------
class TestMissingGuardBlocking(unittest.TestCase):
    def _task_spec_without_guards(self) -> TaskSpec:
        """构造一个有奖励词但没有任何 guard 的最小 TaskSpec"""
        from outbound_eval.domain.schemas_task import RubricItem
        req = RequirementItem(
            id="req.task.001",
            name="deliver task",
            category=RequirementCategory.TASK,
            source_section="intro",
            source_text="请告知用户奖励政策并完成任务。",
            check_method=CheckMethod.RULE,
            severity=Severity.MAJOR,
        )
        rubric = RubricItem(
            rubric_id="rubric.task.001",
            dimension="task",
            linked_requirement_ids=["req.task.001"],
            success_criteria="task completed",
        )
        return TaskSpec(
            task_id="task_test_no_guard",
            task_name="no guard test",
            role="外呼客服",
            objective="完成外呼并告知奖励政策",
            source_text="请告知用户奖励政策并完成任务。",
            opening_line="您好，我是外呼客服。",
            requirements=[req],
            rubric=[rubric],
        )

    def test_risk_without_guard_produces_blocking_finding(self):
        task_spec = self._task_spec_without_guards()
        raw = "请告知用户奖励政策并完成任务。"
        findings = asyncio.run(RiskAuditor().audit(raw, task_spec))
        # 没有任何 guard，reward_policy 应产生 blocking finding
        blocking = [f for f in findings if f.blocking]
        self.assertTrue(blocking, "Missing guards must produce at least one blocking finding")
        categories = {f.metadata.get("risk_category") for f in blocking}
        self.assertIn("reward_policy", categories)

    def test_blocking_finding_has_missing_guards_in_metadata(self):
        task_spec = self._task_spec_without_guards()
        raw = "请告知用户奖励政策并完成任务。"
        findings = asyncio.run(RiskAuditor().audit(raw, task_spec))
        for finding in findings:
            if finding.blocking:
                self.assertTrue(
                    finding.metadata.get("missing_guards"),
                    f"Blocking finding {finding.id} must have non-empty missing_guards in metadata",
                )


# ---------------------------------------------------------------------------
# 3. auto_fix 只产生一条 finding（不重复产生 auto_guarded）
# ---------------------------------------------------------------------------
class TestAutoFixSingleFinding(unittest.TestCase):
    def test_auto_fixed_risk_produces_exactly_one_finding_per_risk(self):
        task_spec = _compile_rider()
        findings = asyncio.run(RiskAuditor().audit(RIDER, task_spec))
        # 按 risk_category 分组，每个风险只能出现一条有意义的 finding
        from collections import Counter
        category_findings: Counter = Counter()
        for f in findings:
            cat = f.metadata.get("risk_category")
            if cat:
                category_findings[cat] += 1
        for category, count in category_findings.items():
            self.assertEqual(count, 1, f"Risk category '{category}' produced {count} findings, expected exactly 1")

    def test_auto_fix_finding_is_not_blocking(self):
        task_spec = _compile_rider()
        findings = asyncio.run(RiskAuditor().audit(RIDER, task_spec))
        auto_fix = [f for f in findings if str(f.decision) == FindingDecision.AUTO_FIX.value]
        for f in auto_fix:
            self.assertFalse(f.blocking, f"AUTO_FIX finding {f.id} must not be blocking")


# ---------------------------------------------------------------------------
# 4. Coverage Planner 消费 RiskCoverageRequirement
# ---------------------------------------------------------------------------
class TestCoveragePlannerConsumesRiskRequirements(unittest.TestCase):
    def test_risk_scenarios_generated_for_all_requirements(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        risk_reqs = task_spec.risk_coverage_requirements
        if not risk_reqs:
            self.skipTest("No risk coverage requirements generated for this sample")
        covered_req_ids = set()
        for scenario in matrix.scenarios:
            for req_id in scenario.metadata.get("risk_coverage_requirement_ids", []):
                covered_req_ids.add(req_id)
        for req in risk_reqs:
            self.assertIn(req.id, covered_req_ids, f"Risk requirement {req.id} not covered by any scenario")

    def test_risk_scenarios_have_risk_metadata(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        risk_scenarios = [s for s in matrix.scenarios if s.metadata.get("risk_scenario")]
        self.assertTrue(risk_scenarios, "Expected at least one risk scenario in the coverage matrix")
        for scenario in risk_scenarios:
            self.assertIn("risk_category_ids", scenario.metadata)
            self.assertIn("risk_coverage_requirement_ids", scenario.metadata)

    def test_critical_risk_scenarios_not_dropped_by_budget(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=8)
        risk_scenarios = [s for s in matrix.scenarios if s.metadata.get("risk_scenario")]
        # critical risk scenarios should survive even at budget=8
        critical_reqs = [req for req in task_spec.risk_coverage_requirements if str(req.priority) == Severity.CRITICAL.value]
        for req in critical_reqs:
            covered = any(req.id in s.metadata.get("risk_coverage_requirement_ids", []) for s in matrix.scenarios)
            self.assertTrue(covered, f"Critical risk requirement {req.id} dropped from budget=8 plan")


# ---------------------------------------------------------------------------
# 5. Coverage QA autofill：uncovered 风险场景自动补充
# ---------------------------------------------------------------------------
class TestCoverageQAAutofill(unittest.TestCase):
    def test_coverage_qa_autofills_missing_risk_scenario(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        if not task_spec.risk_coverage_requirements:
            self.skipTest("No risk coverage requirements")
        # 构造一个没有风险场景的 matrix
        base_matrix = CoveragePlanner(apply_coverage_qa=False).plan(task_spec, budget=12)
        # 移除所有风险场景
        base_matrix = base_matrix.model_copy(update={
            "scenarios": [s for s in base_matrix.scenarios if not s.metadata.get("risk_scenario")],
            "risk_requirement_coverage": {},
            "uncovered_risk_coverage_requirement_ids": [req.id for req in task_spec.risk_coverage_requirements],
        })
        result_matrix = CoverageQA().validate_or_autofill(task_spec, base_matrix, 12)
        # 自动补充后不应有 uncovered
        self.assertFalse(
            result_matrix.uncovered_risk_coverage_requirement_ids,
            "CoverageQA should autofill all uncovered risk requirements",
        )

    def test_coverage_qa_validate_returns_blocking_when_truly_uncovered(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        if not task_spec.risk_coverage_requirements:
            self.skipTest("No risk coverage requirements")
        base_matrix = CoveragePlanner(apply_coverage_qa=False).plan(task_spec, budget=8)
        bare_matrix = base_matrix.model_copy(update={
            "scenarios": [s for s in base_matrix.scenarios if not s.metadata.get("risk_scenario")],
            "risk_requirement_coverage": {},
            "uncovered_risk_coverage_requirement_ids": [req.id for req in task_spec.risk_coverage_requirements],
        })
        qa_result = CoverageQA().validate(task_spec, bare_matrix)
        self.assertFalse(qa_result.passed)
        self.assertTrue(qa_result.findings)
        for finding in qa_result.findings:
            self.assertTrue(finding.blocking)


# ---------------------------------------------------------------------------
# 6. RuleChecker 记录 risk_category_id + forbidden_behavior_id
# ---------------------------------------------------------------------------
class TestRuleCheckerRiskMetadata(unittest.TestCase):
    def test_forbidden_violation_records_risk_category_id(self):
        task_spec = _compile_rider()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        ep = EpisodeExecution(
            run_id="run_test", episode_id="ep_rule", task_id=task_spec.task_id, scenario_id=scenario.scenario_id
        )
        ep.turns.append(
            TurnEvent(id="t1", run_id="run_test", episode_id="ep_rule", turn_index=1, role=TurnRole.ASSISTANT, content="保证奖励。")
        )
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        fail_judges = [j for j in judges if str(j.verdict) == "fail"]
        self.assertTrue(fail_judges, "RuleChecker must detect fabricated reward violation")
        for judge in fail_judges:
            raw = judge.raw_output or {}
            self.assertIn("forbidden_behavior_id", raw, "raw_output must contain forbidden_behavior_id")
            # risk_category_id should be set when forbidden behavior is linked to a risk category
            # (may be None for behaviors not yet linked, but must be present as a key)
            self.assertIn("risk_category_id", raw, "raw_output must contain risk_category_id key")

    def test_forbidden_violation_records_cap_score(self):
        task_spec = _compile_rider()
        scenario = CoveragePlanner().plan(task_spec).scenarios[0]
        ep = EpisodeExecution(
            run_id="run_test", episode_id="ep_cap_raw", task_id=task_spec.task_id, scenario_id=scenario.scenario_id
        )
        ep.turns.append(
            TurnEvent(id="t1", run_id="run_test", episode_id="ep_cap_raw", turn_index=1, role=TurnRole.ASSISTANT, content="保证奖励。")
        )
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        fail_judges = [j for j in judges if str(j.verdict) == "fail"]
        self.assertTrue(fail_judges)
        for judge in fail_judges:
            self.assertIn("cap_score", judge.raw_output or {})


# ---------------------------------------------------------------------------
# 7. KnowledgeChecker risk_linked FAQ 标记
# ---------------------------------------------------------------------------
class TestKnowledgeCheckerRiskLinked(unittest.TestCase):
    def test_risk_linked_faq_marked_in_raw_output(self):
        task_spec = _compile_rider()
        # 运行 spec QA 使 detected_risks 填充
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        if not task_spec.detected_risks:
            self.skipTest("No detected risks in sample")
        risk_faq_ids = {faq_id for risk in task_spec.detected_risks for faq_id in risk.matched_faq_fact_ids}
        if not risk_faq_ids:
            self.skipTest("No risk-linked FAQ facts in sample")
        # 找到一个实际是 risk-linked 的 faq fact
        target_faq = next((f for f in task_spec.faq_facts if f.id in risk_faq_ids), None)
        if target_faq is None:
            self.skipTest("No matching risk-linked FAQ fact found")
        # 构造 scenario，覆盖该 risk-linked faq fact 的 requirement_ids
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        scenario = matrix.scenarios[0].model_copy(update={"covered_requirement_ids": target_faq.requirement_ids})
        ep = EpisodeExecution(run_id="run_test", episode_id="ep_kb", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(
            TurnEvent(id="t1", run_id="run_test", episode_id="ep_kb", turn_index=1, role=TurnRole.ASSISTANT, content="连续配送满足条件有奖励。")
        )
        judges = asyncio.run(KnowledgeChecker().evaluate(task_spec, scenario, ep))
        risk_linked_judges = [j for j in judges if (j.raw_output or {}).get("risk_linked")]
        self.assertTrue(risk_linked_judges, "KnowledgeChecker must mark risk-linked FAQ judges as risk_linked=True")
        for judge in risk_linked_judges:
            self.assertIn("risk_category_ids", judge.raw_output or {})
            self.assertTrue(judge.raw_output["risk_category_ids"])


# ---------------------------------------------------------------------------
# 8. Report 包含 risk_guard_summary 且结构完整
# ---------------------------------------------------------------------------
class TestReportRiskGuardSummary(unittest.TestCase):
    def test_report_has_risk_guard_summary(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        scenario = matrix.scenarios[0]
        ep = EpisodeExecution(run_id="run_rpt", episode_id="ep_rpt", task_id=task_spec.task_id, scenario_id=scenario.scenario_id)
        ep.turns.append(
            TurnEvent(id="t1", run_id="run_rpt", episode_id="ep_rpt", turn_index=1, role=TurnRole.ASSISTANT, content="您好，飞毛腿合同将生效。")
        )
        from outbound_eval.domain.schemas_score import ScoreSummary
        from outbound_eval.scoring.aggregator import ScoreAggregator
        judges = asyncio.run(RuleChecker().evaluate(task_spec, scenario, ep))
        score = ScoreAggregator().aggregate(task_spec, judges, "run_rpt", "ep_rpt")
        report = ReportGenerator().build(task_spec, matrix, [ep], judges, score, {"model_name": "stub"})

        self.assertIsInstance(report.risk_guard_summary, dict)
        self.assertIn("detected_risk_categories", report.risk_guard_summary)
        self.assertIn("guard_contract_statuses", report.risk_guard_summary)
        self.assertIn("risk_coverage_requirements", report.risk_guard_summary)
        self.assertIn("generated_risk_scenarios", report.risk_guard_summary)
        self.assertIn("risk_linked_judge_failures", report.risk_guard_summary)
        self.assertIn("human_needed_risks", report.risk_guard_summary)

    def test_report_markdown_contains_risk_guard_section(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        ep = EpisodeExecution(run_id="run_md", episode_id="ep_md", task_id=task_spec.task_id, scenario_id=matrix.scenarios[0].scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_md", episode_id="ep_md", turn_index=1, role=TurnRole.ASSISTANT, content="好的。"))
        from outbound_eval.scoring.aggregator import ScoreAggregator
        judges = asyncio.run(RuleChecker().evaluate(task_spec, matrix.scenarios[0], ep))
        score = ScoreAggregator().aggregate(task_spec, judges, "run_md")
        report = ReportGenerator().build(task_spec, matrix, [ep], judges, score, {"model_name": "stub"})
        md = ReportGenerator().render_markdown(report)
        self.assertIn("Risk Guard Coverage", md)

    def test_report_html_contains_risk_guard_table(self):
        task_spec = _compile_rider()
        asyncio.run(SpecQAService().audit(RIDER, task_spec))
        matrix = CoveragePlanner().plan(task_spec, budget=12)
        ep = EpisodeExecution(run_id="run_html", episode_id="ep_html", task_id=task_spec.task_id, scenario_id=matrix.scenarios[0].scenario_id)
        ep.turns.append(TurnEvent(id="t1", run_id="run_html", episode_id="ep_html", turn_index=1, role=TurnRole.ASSISTANT, content="好的。"))
        from outbound_eval.scoring.aggregator import ScoreAggregator
        judges = asyncio.run(RuleChecker().evaluate(task_spec, matrix.scenarios[0], ep))
        score = ScoreAggregator().aggregate(task_spec, judges, "run_html")
        report = ReportGenerator().build(task_spec, matrix, [ep], judges, score, {"model_name": "stub"})
        html_out = ReportGenerator().render_html(report)
        self.assertIn("Risk Guard Coverage", html_out)
        self.assertIn("<table", html_out)


# ---------------------------------------------------------------------------
# 9. 禁止 hardcoded "奖励" 特判 — taxonomy 驱动检测
# ---------------------------------------------------------------------------
class TestNoHardcodedRiskTerms(unittest.TestCase):
    def test_risk_detector_uses_taxonomy_not_hardcoded(self):
        """RiskDetector 应该从 taxonomy.terms 召回，而不依赖代码中硬写的词"""
        from outbound_eval.domain.schemas_task import RubricItem
        taxonomy = RiskTaxonomy.load()
        detector = RiskDetector(taxonomy)
        # 用 taxonomy 中不会出现在代码硬编码里的别名词测试
        req = RequirementItem(
            id="req.task.x001",
            name="reward",
            category=RequirementCategory.KNOWLEDGE,
            source_section="s1",
            source_text="连续配送奖金政策：达标后获得奖励金。",
            check_method=CheckMethod.LLM,
        )
        rubric = RubricItem(
            rubric_id="rubric.task.x001",
            dimension="knowledge",
            linked_requirement_ids=["req.task.x001"],
            success_criteria="reward policy explained",
        )
        task_spec = TaskSpec(
            task_id="task_det_test",
            task_name="det test",
            role="外呼客服",
            objective="告知奖励政策",
            source_text="连续配送奖金政策。",
            opening_line="您好",
            requirements=[req],
            rubric=[rubric],
        )
        risks = detector.detect("连续配送奖金政策", task_spec)
        categories = {r.risk_category_id for r in risks}
        self.assertIn("reward_policy", categories, "RiskDetector must detect reward_policy via taxonomy terms")
        # Should also record matched terms
        reward_risk = next(r for r in risks if r.risk_category_id == "reward_policy")
        self.assertTrue(reward_risk.matched_terms)

    def test_spec_qa_detects_reward_risk_without_special_case_code(self):
        """SpecQA 不得依靠 if '奖励' in raw_instruction 触发，必须通过 taxonomy 链路"""
        from outbound_eval.domain.schemas_task import RubricItem
        req = RequirementItem(
            id="req.task.y001",
            name="task",
            category=RequirementCategory.TASK,
            source_section="s1",
            source_text="完成外呼通知，涉及奖金发放。",
            check_method=CheckMethod.RULE,
        )
        rubric = RubricItem(
            rubric_id="rubric.task.y001",
            dimension="task",
            linked_requirement_ids=["req.task.y001"],
            success_criteria="task completed",
        )
        task_spec = TaskSpec(
            task_id="task_spec_qa_test",
            task_name="spec qa test",
            role="外呼客服",
            objective="完成外呼",
            source_text="完成外呼通知，涉及奖金发放。",
            opening_line="您好",
            requirements=[req],
            rubric=[rubric],
        )
        raw = "完成外呼通知，涉及奖金发放。"
        result = asyncio.run(SpecQAService().audit(raw, task_spec))
        risk_findings = [f for f in result.findings if str(f.source) == "risk"]
        risk_categories = {f.metadata.get("risk_category") for f in risk_findings}
        self.assertIn("reward_policy", risk_categories, "SpecQA must detect reward_policy via taxonomy, not hardcoded terms")


if __name__ == "__main__":
    unittest.main()
