from enum import Enum


class RequirementCategory(str, Enum):
    TASK = "task"
    FLOW = "flow"
    KNOWLEDGE = "knowledge"
    CONSTRAINT = "constraint"
    EXCEPTION = "exception"
    TERMINATION = "termination"


class CheckMethod(str, Enum):
    RULE = "rule"
    FLOW = "flow"
    KNOWLEDGE = "knowledge"
    LLM = "llm"
    HYBRID = "hybrid"


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NONE = "none"


class ScenarioType(str, Enum):
    HAPPY_PATH = "happy_path"
    MAIN_FLOW = "main_flow"
    EXCEPTION = "exception"
    FAQ_PROBE = "faq_probe"
    CONSTRAINT_RISK = "constraint_risk"
    BRANCH = "branch"
    ADVERSARIAL = "adversarial"
    METAMORPHIC = "metamorphic"


class Verdict(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    NOT_TESTED = "not_tested"


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class EpisodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingSource(str, Enum):
    COMPLETENESS = "completeness"
    AMBIGUITY = "ambiguity"
    RISK = "risk"


class FindingDecision(str, Enum):
    AUTO_FIX = "auto_fix"
    AUTO_GUARDED = "auto_guarded"
    HUMAN_NEEDED = "human_needed"
    DEFER = "defer"
    DISMISS = "dismiss"


class RiskGuardType(str, Enum):
    FAQ_GROUNDING = "faq_grounding"
    KNOWLEDGE_REQUIREMENT = "knowledge_requirement"
    FLOW_REQUIREMENT = "flow_requirement"
    EXCEPTION_REQUIREMENT = "exception_requirement"
    CONSTRAINT_RULE = "constraint_rule"
    TERMINATION_RULE = "termination_rule"
    FORBIDDEN_FABRICATION = "forbidden_fabrication"
    FORBIDDEN_COMMITMENT = "forbidden_commitment"
    FORBIDDEN_WRONG_GUIDANCE = "forbidden_wrong_guidance"
    FORBIDDEN_OVERCLAIM = "forbidden_overclaim"
    RUBRIC_ITEM = "rubric_item"
    SEVERITY_CAP = "severity_cap"
    COVERAGE_REQUIREMENT = "coverage_requirement"


class EventType(str, Enum):
    TASK_COMPILED = "TaskCompiledEvent"
    SPEC_QA_FINDING = "SpecQAFindingEvent"
    SCENARIO_GENERATED = "ScenarioGeneratedEvent"
    EPISODE_STARTED = "EpisodeStartedEvent"
    TURN = "TurnEvent"
    SIMULATOR_ACTION = "SimulatorActionEvent"
    MODEL_CALL = "ModelCallEvent"
    CHECKER_STARTED = "CheckerStartedEvent"
    JUDGE = "JudgeEvent"
    SCORE_AGGREGATED = "ScoreAggregatedEvent"
    REPORT_GENERATED = "ReportGeneratedEvent"
    SYSTEM_ERROR = "SystemErrorEvent"
