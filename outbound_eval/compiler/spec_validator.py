from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from outbound_eval.domain.schemas_task import TaskSpec


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str
    path: str
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class TaskSpecValidator:
    def validate(self, data: TaskSpec | dict) -> ValidationReport:
        issues: list[ValidationIssue] = []
        try:
            spec = data if isinstance(data, TaskSpec) else TaskSpec.model_validate(data)
        except ValidationError as error:
            for item in error.errors():
                issues.append(
                    ValidationIssue(level="ERROR", path=".".join(str(p) for p in item["loc"]), message=item["msg"])
                )
            return self._report(issues)

        req_ids = {req.id for req in spec.requirements}
        if not spec.opening_line:
            issues.append(ValidationIssue(level="WARNING", path="opening_line", message="opening_line is empty"))
        for req in spec.requirements:
            if not req.source_text.strip():
                issues.append(ValidationIssue(level="ERROR", path=req.id, message="requirement lacks source_text"))
        for fact in spec.faq_facts:
            if not fact.grounding_source.strip():
                issues.append(ValidationIssue(level="ERROR", path=fact.id, message="FAQFact lacks grounding_source"))
        for node in spec.flow_nodes:
            if not node.requirement_ids and not spec.branch_rules:
                issues.append(ValidationIssue(level="WARNING", path=node.id, message="flow node has no requirement or branch"))
            for req_id in node.requirement_ids:
                if req_id not in req_ids:
                    issues.append(ValidationIssue(level="ERROR", path=node.id, message=f"unknown requirement {req_id}"))
        return self._report(issues)

    def _report(self, issues: list[ValidationIssue]) -> ValidationReport:
        summary = {
            "errors": sum(1 for item in issues if item.level == "ERROR"),
            "warnings": sum(1 for item in issues if item.level == "WARNING"),
            "info": sum(1 for item in issues if item.level == "INFO"),
        }
        return ValidationReport(valid=summary["errors"] == 0, issues=issues, summary=summary)

