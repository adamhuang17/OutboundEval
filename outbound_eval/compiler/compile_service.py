from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from outbound_eval.compiler.llm_task_compiler import LLMTaskCompiler
from outbound_eval.compiler.spec_validator import TaskSpecValidator, ValidationReport
from outbound_eval.domain.schemas_model import ModelConfig
from outbound_eval.domain.schemas_task import TaskSpec
from outbound_eval.domain.schemas_understanding import TaskUnderstanding


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
    understanding: TaskUnderstanding | None = None


class InstructionCompileService:
    def __init__(self, compiler: LLMTaskCompiler | None = None, validator: TaskSpecValidator | None = None):
        self.compiler = compiler or LLMTaskCompiler()
        self.validator = validator or TaskSpecValidator()

    def compile(self, raw_instruction: str, model_config: ModelConfig | None = None, repair_attempts: int = 0) -> CompileResult:
        if model_config is None:
            report = ValidationReport(valid=False, issues=[], summary={"errors": 1, "warnings": 0, "info": 0})
            return CompileResult(
                status="failed",
                validation_report=report,
                compile_error=CompileError(
                    message="InstructionCompileService now requires model_config and uses LLMTaskCompiler. Legacy rule extraction is disabled.",
                    attempts=0,
                    validation_report=report,
                ),
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acompile(raw_instruction, model_config=model_config, repair_attempts=repair_attempts))
        raise RuntimeError("Use await InstructionCompileService().acompile(...) inside an active event loop.")

    async def acompile(self, raw_instruction: str, *, model_config: ModelConfig, repair_attempts: int = 0) -> CompileResult:
        try:
            understanding = await self.compiler.compile(raw_markdown=raw_instruction, model_config=model_config)
            task_spec = TaskSpec.model_validate(understanding.task_spec)
            report = self.validator.validate(task_spec)
            if report.valid and not any(f.blocking for f in understanding.compile_findings):
                return CompileResult(status="ok", task_spec=task_spec, validation_report=report, understanding=understanding)
            return CompileResult(
                status="failed",
                task_spec=task_spec if report.valid else None,
                validation_report=report,
                understanding=understanding,
                compile_error=CompileError(
                    message="TaskUnderstanding failed validation or CompileQAGate.",
                    attempts=repair_attempts + 1,
                    validation_report=report,
                    raw_output=understanding.model_dump(mode="json"),
                ),
            )
        except Exception as exc:
            report = ValidationReport(valid=False, issues=[], summary={"errors": 1, "warnings": 0, "info": 0})
            return CompileResult(
                status="failed",
                validation_report=report,
                compile_error=CompileError(message=str(exc), attempts=repair_attempts + 1, validation_report=report),
            )

    def write_outputs(self, result: CompileResult, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if result.task_spec:
            (out_dir / "task_spec.json").write_text(result.task_spec.model_dump_json(indent=2), encoding="utf-8")
        if result.understanding:
            (out_dir / "task_understanding.json").write_text(result.understanding.model_dump_json(indent=2), encoding="utf-8")
        if result.compile_error:
            (out_dir / "compile_error.json").write_text(result.compile_error.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "validation_report.json").write_text(result.validation_report.model_dump_json(indent=2), encoding="utf-8")

    def _repair_instruction(self, raw_instruction: str, report: ValidationReport) -> str:
        missing_opening = any(issue.path == "opening_line" for issue in report.issues)
        if missing_opening and "# Opening Line" not in raw_instruction:
            return raw_instruction.rstrip() + "\n\n# Opening Line\n您好，我是本次业务通知客服。\n"
        return raw_instruction
