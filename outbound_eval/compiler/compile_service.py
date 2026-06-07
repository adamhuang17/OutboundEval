from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.compiler.llm_spec_extractor import RuleBasedSpecExtractor, TaskSpecDraft
from outbound_eval.compiler.spec_normalizer import normalize_task_spec
from outbound_eval.compiler.spec_validator import TaskSpecValidator, ValidationReport
from outbound_eval.domain.schemas_task import TaskSpec


class CompileError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    attempts: int
    validation_report: ValidationReport | None = None
    raw_output: dict[str, Any] | None = None


class CompileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    task_spec: TaskSpec | None = None
    validation_report: ValidationReport
    compile_error: CompileError | None = None
    draft: TaskSpecDraft | None = None


class InstructionCompileService:
    def __init__(self, extractor: RuleBasedSpecExtractor | None = None, validator: TaskSpecValidator | None = None):
        self.extractor = extractor or RuleBasedSpecExtractor()
        self.validator = validator or TaskSpecValidator()

    def compile(self, raw_instruction: str, repair_attempts: int = 2) -> CompileResult:
        last_report: ValidationReport | None = None
        draft: TaskSpecDraft | None = None
        for attempt in range(repair_attempts + 1):
            try:
                draft = self.extractor.extract(raw_instruction)
                task_spec = normalize_task_spec(raw_instruction, draft)
                report = self.validator.validate(task_spec)
                last_report = report
                if report.valid:
                    return CompileResult(status="ok", task_spec=task_spec, validation_report=report, draft=draft)
                raw_instruction = self._repair_instruction(raw_instruction, report)
            except Exception as exc:
                last_report = ValidationReport(
                    valid=False,
                    issues=[],
                    summary={"errors": 1, "warnings": 0, "info": 0},
                )
                if attempt >= repair_attempts:
                    return CompileResult(
                        status="failed",
                        validation_report=last_report,
                        draft=draft,
                        compile_error=CompileError(message=str(exc), attempts=attempt + 1, validation_report=last_report),
                    )
        assert last_report is not None
        return CompileResult(
            status="failed",
            validation_report=last_report,
            draft=draft,
            compile_error=CompileError(message="TaskSpec failed validation after repair", attempts=repair_attempts + 1, validation_report=last_report),
        )

    def write_outputs(self, result: CompileResult, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if result.task_spec:
            (out_dir / "task_spec.json").write_text(result.task_spec.model_dump_json(indent=2), encoding="utf-8")
        if result.compile_error:
            (out_dir / "compile_error.json").write_text(result.compile_error.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "validation_report.json").write_text(result.validation_report.model_dump_json(indent=2), encoding="utf-8")

    def _repair_instruction(self, raw_instruction: str, report: ValidationReport) -> str:
        missing_opening = any(issue.path == "opening_line" for issue in report.issues)
        if missing_opening and "# Opening Line" not in raw_instruction:
            return raw_instruction.rstrip() + "\n\n# Opening Line\n您好，我是本次业务通知客服。\n"
        return raw_instruction

