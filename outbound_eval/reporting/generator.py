from __future__ import annotations

import html
from pathlib import Path

from outbound_eval.domain.schemas_episode import EpisodeExecution
from outbound_eval.domain.schemas_judge import JudgeEvent
from outbound_eval.domain.schemas_report import ReportArtifact
from outbound_eval.domain.schemas_scenario import CoverageMatrix
from outbound_eval.domain.schemas_score import ScoreSummary
from outbound_eval.domain.schemas_task import TaskSpec


class ReportGenerator:
    def build(
        self,
        task_spec: TaskSpec,
        coverage: CoverageMatrix,
        episodes: list[EpisodeExecution],
        judges: list[JudgeEvent],
        score: ScoreSummary,
        model_summary: dict,
    ) -> ReportArtifact:
        turn_index = {turn.id: turn for episode in episodes for turn in episode.turns}
        failed = [judge for judge in judges if str(judge.verdict) in {"fail", "partial"}]
        evidence_index = {
            turn_id: {
                "episode_id": turn_index[turn_id].episode_id,
                "turn_id": turn_id,
                "quote": turn_index[turn_id].content,
            }
            for judge in failed
            for turn_id in judge.evidence_turn_ids
            if turn_id in turn_index
        }
        return ReportArtifact(
            run_id=score.run_id,
            task_summary={"task_id": task_spec.task_id, "task_name": task_spec.task_name, "requirements": len(task_spec.requirements)},
            model_summary=model_summary,
            coverage_summary={
                "requirement_coverage_rate": coverage.requirement_coverage_rate,
                "requirement_coverage": coverage.requirement_coverage,
                "flow_node_coverage": coverage.flow_node_coverage,
                "branch_coverage": coverage.branch_coverage,
                "faq_coverage": coverage.faq_coverage,
                "risk_coverage": coverage.risk_coverage,
                "uncovered_requirement_ids": coverage.uncovered_requirement_ids,
            },
            score_summary=score.model_dump(mode="json"),
            severity_caps=[cap.model_dump(mode="json") for cap in score.caps_applied],
            risk_guard_summary=self._build_risk_guard_summary(task_spec, coverage, judges),
            episode_summaries=[
                {
                    "episode_id": ep.episode_id,
                    "scenario_id": ep.scenario_id,
                    "status": ep.status,
                    "turns": len(ep.turns),
                    "termination_reason": ep.termination_reason,
                }
                for ep in episodes
            ],
            failed_items=[
                {
                    "judge_id": judge.id,
                    "requirement_id": judge.requirement_id,
                    "rubric_item_id": judge.rubric_item_id,
                    "episode_id": judge.episode_id,
                    "turn_ids": judge.evidence_turn_ids,
                    "verdict": judge.verdict,
                    "severity": judge.severity,
                    "reason": judge.reason,
                    "evidence_quotes": judge.evidence_quotes,
                }
                for judge in failed
            ],
            evidence_index=evidence_index,
            improvement_suggestions=[
                {
                    "requirement_id": judge.requirement_id,
                    "episode_id": judge.episode_id,
                    "turn_ids": judge.evidence_turn_ids,
                    "suggestion": f"Revise behavior for {judge.requirement_id}: {judge.reason}",
                }
                for judge in failed
            ],
        )

    def _build_risk_guard_summary(
        self,
        task_spec: TaskSpec,
        coverage: CoverageMatrix,
        judges: list[JudgeEvent],
    ) -> dict:
        risk_judge_failures: dict[str, list[str]] = {}
        for judge in judges:
            if str(judge.verdict) not in {"fail", "partial"}:
                continue
            raw = judge.raw_output or {}
            category = raw.get("risk_category_id")
            if category:
                risk_judge_failures.setdefault(category, []).append(judge.id)

        risk_scenarios: dict[str, list[str]] = {}
        for scenario in coverage.scenarios:
            for req_id in scenario.metadata.get("risk_coverage_requirement_ids", []):
                risk_scenarios.setdefault(str(req_id), []).append(scenario.scenario_id)

        guard_statuses = [status.model_dump(mode="json") for status in task_spec.risk_guard_statuses]
        detected = [risk.model_dump(mode="json") for risk in task_spec.detected_risks]
        coverage_reqs = [req.model_dump(mode="json") for req in task_spec.risk_coverage_requirements]
        human_needed = [
            status for status in guard_statuses if status.get("missing_guards")
        ]

        return {
            "detected_risk_categories": detected,
            "guard_contract_statuses": guard_statuses,
            "risk_coverage_requirements": coverage_reqs,
            "generated_risk_scenarios": risk_scenarios,
            "risk_linked_judge_failures": risk_judge_failures,
            "human_needed_risks": human_needed,
        }

    def write(self, artifact: ReportArtifact, out_dir: Path | str) -> dict[str, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / "report.json"
        md_path = out / "report.md"
        html_path = out / "report.html"
        json_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        md = self.render_markdown(artifact)
        md_path.write_text(md, encoding="utf-8")
        html_path.write_text(self.render_html(artifact, md), encoding="utf-8")
        return {"json": json_path, "markdown": md_path, "html": html_path}

    def render_markdown(self, artifact: ReportArtifact) -> str:
        lines = [
            f"# OutboundEval Report {artifact.run_id}",
            "",
            f"- Task: {artifact.task_summary.get('task_name')}",
            f"- Score: {artifact.score_summary.get('normalized_score')}",
            f"- Requirement coverage: {artifact.coverage_summary.get('requirement_coverage_rate'):.2%}",
            "",
            "## Failed / Partial Items",
        ]
        for item in artifact.failed_items:
            lines.append(
                f"- `{item['requirement_id']}` `{item['episode_id']}` turns={item['turn_ids']} verdict={item['verdict']}: {item['reason']}"
            )
        lines.append("")
        lines.append("## Improvement Suggestions")
        for item in artifact.improvement_suggestions:
            lines.append(f"- `{item['requirement_id']}`: {item['suggestion']}")

        rgs = artifact.risk_guard_summary
        if rgs:
            lines += ["", "## Risk Guard Coverage", ""]
            detected = rgs.get("detected_risk_categories", [])
            if detected:
                lines.append(f"### Detected Risk Categories ({len(detected)})")
                for risk in detected:
                    lines.append(f"- **{risk['risk_category_id']}** matched_terms={risk.get('matched_terms', [])}")
            statuses = rgs.get("guard_contract_statuses", [])
            if statuses:
                lines += ["", "### Guard Contract Status"]
                for status in statuses:
                    badge = "auto_guarded ✓" if not status.get("missing_guards") else f"missing: {status['missing_guards']}"
                    lines.append(f"- **{status['risk_category_id']}**: {badge}")
                    if status.get("present_guards"):
                        lines.append(f"  - present: {status['present_guards']}")
            reqs = rgs.get("risk_coverage_requirements", [])
            if reqs:
                lines += ["", "### Risk Coverage Requirements"]
                for req in reqs:
                    lines.append(f"- `{req['id']}` priority={req['priority']} min_scenarios={req['min_scenarios']}")
            scenarios = rgs.get("generated_risk_scenarios", {})
            if scenarios:
                lines += ["", "### Generated Risk Scenarios"]
                for req_id, scenario_ids in scenarios.items():
                    lines.append(f"- `{req_id}`: {scenario_ids}")
            failures = rgs.get("risk_linked_judge_failures", {})
            if failures:
                lines += ["", "### Risk-linked Judge Failures"]
                for category, judge_ids in failures.items():
                    lines.append(f"- `{category}`: {judge_ids}")
            caps = artifact.severity_caps
            if caps:
                lines += ["", "### Severity Caps Triggered"]
                for cap in caps:
                    lines.append(f"- {cap.get('risk_category_id', 'unknown')} cap={cap['cap_score']} reason={cap['reason']}")
            human_needed = rgs.get("human_needed_risks", [])
            if human_needed:
                lines += ["", "### Remaining Human-needed Risks"]
                for status in human_needed:
                    lines.append(f"- **{status['risk_category_id']}** missing_guards={status['missing_guards']}")

        return "\n".join(lines)

    def render_html(self, artifact: ReportArtifact, markdown_text: str | None = None) -> str:
        items = "\n".join(
            f"<li><a href='#turn-{html.escape(turn_id)}'>{html.escape(str(item['requirement_id']))}</a> "
            f"{html.escape(str(item['verdict']))}: {html.escape(item['reason'])}</li>"
            for item in artifact.failed_items
            for turn_id in (item.get("turn_ids") or [""])
        )
        evidence = "\n".join(
            f"<section id='turn-{html.escape(turn_id)}'><h3>{html.escape(turn_id)}</h3><p>{html.escape(data['quote'])}</p></section>"
            for turn_id, data in artifact.evidence_index.items()
        )
        rgs = artifact.risk_guard_summary
        risk_section = ""
        if rgs:
            detected = rgs.get("detected_risk_categories", [])
            guard_rows = "".join(
                f"<tr><td><code>{html.escape(s['risk_category_id'])}</code></td>"
                f"<td>{'✓ guarded' if not s.get('missing_guards') else '✗ missing: ' + html.escape(str(s['missing_guards']))}</td>"
                f"<td>{html.escape(str(s.get('present_guards', [])))}</td></tr>"
                for s in rgs.get("guard_contract_statuses", [])
            )
            human_rows = "".join(
                f"<li><code>{html.escape(s['risk_category_id'])}</code> missing: {html.escape(str(s['missing_guards']))}</li>"
                for s in rgs.get("human_needed_risks", [])
            )
            risk_section = f"""
<h2>Risk Guard Coverage</h2>
<p>Detected risk categories: <strong>{len(detected)}</strong></p>
<table border="1" cellpadding="4" cellspacing="0">
<thead><tr><th>Risk Category</th><th>Guard Status</th><th>Present Guards</th></tr></thead>
<tbody>{guard_rows}</tbody>
</table>
{'<h3>Human-needed Risks</h3><ul>' + human_rows + '</ul>' if human_rows else ''}
"""
        return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>OutboundEval Report</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:32px;line-height:1.5}}code{{background:#f2f4f7;padding:2px 5px}}section{{border-top:1px solid #ddd;padding-top:12px}}table{{border-collapse:collapse}}th,td{{padding:4px 8px}}</style></head>
<body>
<h1>OutboundEval Report {html.escape(artifact.run_id)}</h1>
<p>Score: <strong>{artifact.score_summary.get('normalized_score')}</strong></p>
<p>Requirement coverage: <strong>{artifact.coverage_summary.get('requirement_coverage_rate'):.2%}</strong></p>
<h2>Failed / Partial Items</h2><ul>{items}</ul>
{risk_section}
<h2>Evidence</h2>{evidence}
</body></html>"""

