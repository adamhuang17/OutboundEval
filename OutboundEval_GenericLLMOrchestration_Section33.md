## 33. 通用 Markdown 任务理解与三类 LLM 编排重构方案

本节用于纠正当前方案的任务理解偏差。比赛系统不应被实现成“飞毛腿/课程直播等内置模板评测器”，也不应让 Risk Guard 成为主流程入口。正确定位是一个通用评测操作台：

```text
评测员输入半结构化 Markdown 任务指令
  -> 统一任务理解层
  -> LLM 场景构建
  -> LLM 用户模拟
  -> 被测对话 LLM 执行任务
  -> LLM + 规则评估
  -> 带证据的评估报告
```

第 32 节的 Risk Guard Coverage 仍然保留，但它应降级为 `TaskSpec / JudgePlan` 之后的防护完整性校验层，而不是产品核心建模方式。核心产品能力应围绕 `任务指令 -> 场景构建 -> 对话模拟 -> 效果评估 -> 评估报告`。

### 33.1 重构后的核心判断

系统里确实需要三类 LLM 角色，但它们不能各自独立理解原始任务。必须先生成统一的结构化理解，再把不同可见子集分发给三类 LLM。

```text
Raw Markdown Task
  -> MarkdownAst
  -> LLMTaskCompiler
  -> TaskSpec + JudgePlan + RiskPlan + SourceMap

TaskSpec / JudgePlan / RiskPlan
  -> ScenarioBuilderLLM
  -> ScenarioSet

ScenarioSet + EvaluatorPersonaInput
  -> UserSimulatorLLM
  -> 多轮用户消息

Raw Task + Task Summary + Visible Conversation
  -> TargetDialogueLLM
  -> 外呼模型回复

JudgePlan + Scenario + Transcript
  -> EvaluationLLM + RuleGuards + ScoreAggregator
  -> ScoreEvidenceReport
```

三类 LLM 的职责边界如下：

| LLM 角色 | 输入 | 输出 | 不允许看到 |
|---|---|---|---|
| `Task Orchestrator / Evaluation LLM` | 原始 Markdown、Markdown AST、评测员配置 | `TaskSpec`、`JudgePlan`、`RiskPlan`、场景集合、评分结果 | 不参与被测模型实时回复 |
| `UserSimulatorLLM` | 任务摘要、场景、评测员画像、隐藏用户目标、对话历史 | 用户下一句话、意图状态、是否继续 | 不应向被测模型泄露 judge 细则、隐藏目标、期望答案 |
| `TargetDialogueLLM` | 原始任务指令、变量、可见对话历史 | 外呼人员回复 | 不能看到场景、画像隐藏目标、评分项、风险要求 |

### 33.2 当前实现偏差与对应代码位置

这些不是抽象问题，而是当前代码里已经存在的工程偏差，实现 Agent 必须优先处理。

| 偏差 | 当前文件路径 | 具体问题 | 重构方向 |
|---|---|---|---|
| Markdown 只做扁平 section 切分 | `D:\OutboundEval-meituan\outbound_eval\compiler\section_splitter.py` | `split_sections()` 返回 `SectionBlock` 列表和 `section_map()`，没有 `children`、line span、bullet 层级；`Step 3 / 3.1 / 4.1` 易丢结构 | 新增 `MarkdownAstParser`，输出层级树和 source map |
| 任务理解不是 LLM 编译 | `D:\OutboundEval-meituan\outbound_eval\compiler\llm_spec_extractor.py` | 类名是 `RuleBasedSpecExtractor`，按 `section_map` 规则抽取；`_derive_task_name()` 里用“骑手/飞毛腿/直播/课程”推任务名 | 新增 `LLMTaskCompiler`；删除业务关键词任务名判断 |
| 知识点结构过窄 | `D:\OutboundEval-meituan\outbound_eval\compiler\llm_spec_extractor.py`、`D:\OutboundEval-meituan\outbound_eval\domain\schemas_task.py` | 现在主要是 `faq_pairs` / `FAQFact(question, answer)`，但比赛输入可能是 bullet 知识点、政策、步骤、定义，不一定是 Q/A | 用 `KnowledgeFact` 统一表达 FAQ、政策、业务规则、配置步骤、定义 |
| Coverage Planner 仍有固定蓝图 | `D:\OutboundEval-meituan\outbound_eval\planner\coverage_planner.py` | `DEFAULT_SCENARIO_BLUEPRINTS` 内置 `normal confirmation / user refusal / driving / boundary question` 等固定场景 | 改成抽象 coverage policy，由 `ScenarioBuilderLLM` 根据任务生成真实场景 |
| 用户动作空间被业务动作污染 | `D:\OutboundEval-meituan\outbound_eval\simulator\action_registry.py` | 默认动作含 `ask_reward_rule`、`ask_exit_method`、`ask_refund`、`ask_privacy` 等业务词 | 这些只作为 fallback；主路径改为 `intent/state/utterance` |
| 用户模拟器渲染固定话术 | `D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py` | `_render()` 将动作映射为固定中文句子；LLM 路径仍要求选择 action_name | `UserSimulatorLLM` 直接输出自然话术和状态，动作 registry 只做降级 |
| 被测模型上下文隔离方向正确但未形成统一理解 | `D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py` | `target_visible_context()` 已隐藏 scenario，但 target 看原始任务，simulator/evaluator 看不完整规则抽取结果 | 让三方共享 `TaskUnderstanding` 的不同可见投影 |
| SemanticJudge 为空实现 | `D:\OutboundEval-meituan\outbound_eval\evaluator\semantic_judge.py` | `evaluate()` 直接返回 `[]`，没有按 JudgePlan 做语义评分 | 实现 `JudgePlan` 驱动的 LLM judge |
| 前端没有评测员画像和场景卡片 | `D:\OutboundEval-meituan\outbound_eval\web\app.py`、`D:\OutboundEval-meituan\outbound_eval\web\static\app.js`、`index.html` | 当前 API 有 `/api/compile`、`/api/plan`、`/api/run`，没有 `/api/scenarios/build`；UI 没有 persona 输入和场景可视化 | 新增画像输入、场景构建按钮、场景卡片、选择/锁定场景后运行 |

### 33.3 本地参考项目源码映射

以下参考路径是实现 Agent 必须阅读的真实代码位置。每个参考不是“照搬”，而是对应一个可落地工程约束。

| 参考项目 | 本地源码路径 | 可借鉴模块 | 应用到 OutboundEval 的方式 |
|---|---|---|---|
| OpenSpec | `D:\Public Project\OpenSpec-main\src\core\parsers\markdown-parser.ts` | `MarkdownParser.parseSections()` 用 stack 构建 `Section.children`，并用 code fence mask 避免误判代码块标题 | `MarkdownAstParser` 应借鉴该结构，保留 `level/title/content/children`，同时增加 line span 和 bullets |
| OpenSpec | `D:\Public Project\OpenSpec-main\src\core\validation\validator.ts` | `Validator` 在 parse 后执行 schema 校验和规则校验；要求每个 requirement 至少有 scenario | `CompileQAGate` 和 `ScenarioQAGate` 应分离：编译后校验 spec，场景后校验 judge point 覆盖 |
| OpenSpec | `D:\Public Project\OpenSpec-main\src\core\schemas\base.schema.ts` | `RequirementSchema` 强约束 requirement 不为空且 scenarios 不为空 | `JudgePoint` / `RequirementItem` 必须有可追溯 source 和至少一个覆盖场景 |
| OpenSpec | `D:\Public Project\OpenSpec-main\src\core\parsers\requirement-blocks.ts` | 保留 raw block、header line、section body 的解析方式 | OutboundEval 的 `SourceMap` 必须保存原文片段，不只保存归一化文本 |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\generator\runner.py` | Planner -> Builder -> Postprocess -> Structural Validation 两段式 LLM 生成 | `LLMTaskCompiler` 可采用 `Analyzer -> Compiler -> Normalize/Validate`；`ScenarioBuilderLLM` 可采用 `Planner -> Builder` |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\generator\types.py` | `WorkflowGenerateErrorCode` 机器可读错误码与人类 detail 分离 | `CompileFinding`、`ScenarioFinding`、`JudgeFinding` 都要有稳定 code，而不是只存自然语言 |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\generator\prompts\planner_prompts.py` | Planner 输出短 JSON scaffold，先决定节点类型与意图 | `ScenarioPlannerLLM` 先输出覆盖计划，不直接生成完整话术 |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\generator\prompts\builder_prompts.py` | Builder 根据 planner scaffold 输出完整 graph，并被 runner 后处理 | `ScenarioBuilderLLM` 根据覆盖计划生成完整 `ScenarioSpec`，再由 QA 修复 id、覆盖、字段 |
| Dify | `D:\Public Project\Dify-main\api\core\llm_generator\output_parser\structured_output.py` | 支持原生 JSON Schema、JSON mode、prompt-based schema、json_repair | `StructuredLLMClient` 应统一封装 structured output、修复、重试、Pydantic 校验 |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\node_runtime.py` | `DifyPreparedLLM.invoke_llm_with_structured_output()` 隔离模型实例细节 | OutboundEval 的 `ModelAdapter` 不要把 OpenAI SDK 泄露给 compiler/simulator/judge |
| Dify | `D:\Public Project\Dify-main\api\core\workflow\workflow_entry.py` | `WorkflowEntry` 通过 graph runtime state、variable pool、observability layer 管理执行 | `EvaluationRun` 应明确 runtime state，阶段输出进入 artifact/store，不靠内存临时变量 |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\tools\registry\views.py` | `RegisteredAction`、`ActionModel`、`ActionRegistry` schema-first 动作注册 | 可保留 `UserActionRegistry` 作为 fallback，但主路径输出不应固定 action name |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\tools\registry\service.py` | `create_action_model()` 动态生成 Pydantic union action schema | 如果后续要支持“可控动作空间”，应按场景动态生成动作 schema，而不是全局固定业务动作 |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\agent\views.py` | `AgentOutput` 同时含 `evaluation_previous_goal / memory / next_goal / action`，支持 loop 检测 | `UserSimulatorOutput` 应含 `state / memory_update / next_intent / utterance / should_continue` |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\agent\service.py` | `_setup_action_models()`、message manager、history、loop detector、fallback LLM | `DialogueManager` 应管理轮次、停止、卡住检测、重试，而不是塞进 simulator prompt |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\agent\judge.py` | judge prompt 明确 task、trajectory、final result、ground truth、JSON response format | `SemanticJudge` 应以 JudgePlan 为 ground truth，输出严格 JSON 并引用 turn evidence |
| BrowserUse | `D:\Public Project\browser-use-main\browser_use\llm\openai\chat.py` | `ainvoke(..., output_format=PydanticModel)` 支持 response_format JSON Schema 与 fallback | OutboundEval 应新增 `StructuredLLMClient.invoke(model, messages, output_model)` |
| RAGFlow | `D:\Public Project\ragflow-main\rag\prompts\citation_prompt.md` | citation 规则要求事实、数字、定义必须引用来源 | 报告里的每个失败项必须引用 `episode_id/turn_id/source_node_id` |
| RAGFlow | `D:\Public Project\ragflow-main\rag\prompts\sufficiency_check.md` | 输出 `is_sufficient/reasoning/missing_information` | `CompileQAGate` 可用相同结构检查任务理解是否足够，不足则生成待确认 finding |
| RAGFlow | `D:\Public Project\ragflow-main\api\db\services\evaluation_service.py` | Dataset -> Case -> Run -> Result -> Summary metrics | OutboundEval 的 `TaskDefinition / ScenarioDefinition / EvaluationRun / EpisodeExecution / JudgeResult` 要按此分层 |
| RAGFlow | `D:\Public Project\ragflow-main\api\apps\restful_apis\agent_api.py` | session 持久化、trace_items、structured_output 合并返回 | Web API 运行结果应返回 transcript、structured judge、trace items，而不仅是报告 URL |
| Monocle | `D:\Public Project\monocle-main\apptrace\src\README.md` | Trace 是 spans 集合，保存应用运行、步骤、耗时、输入输出 | OutboundEval 的每次 LLM 调用、turn、judge item 都应有 trace/span 元数据 |
| Monocle | `D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\exporters\file_exporter.py` | 按 trace_id 分组写 JSON 文件，root span 到达后关闭 trace | `TraceStore` 可用 JSONL/JSON 文件降级实现，Postgres 是增强项 |
| Monocle | `D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\instrumentation\common\span_handler.py` | 给 span 注入默认属性、scope、workflow name、status | `TraceEvent` 必须包含 stage、model、duration、status、token、source ids |
| BMAD | `D:\Public Project\BMAD-METHOD-main\src\core-skills\bmad-review-edge-case-hunter\SKILL.md` | 机械遍历 branching path / boundary condition，只输出未处理边界 | `ScenarioBuilderLLM` 的 coverage prompt 要让模型逐条覆盖 flow/knowledge/constraint/risk，而不是凭直觉生成几条场景 |
| BMAD | `D:\Public Project\BMAD-METHOD-main\src\bmm-skills\4-implementation\bmad-code-review\steps\step-03-triage.md` | 多来源 finding 归一化、去重、分类为 decision/patch/defer/dismiss | `FindingAggregator` 应合并 rule checker、LLM judge、risk auditor 的发现 |
| anthropics-skills | `D:\Public Project\anthropics-skills\skills-main\skills\skill-creator\SKILL.md` | Skill 包由 `SKILL.md + references/scripts/assets/evals` 组织，并可迭代评估 | 外呼任务可以沉淀为可选 task pack，但不能作为系统内置模板；task pack 只是示例和复测资产 |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Schema_First_Block_Pattern.md` | Schema-first 让输入输出驱动 UI、校验、LLM 工具描述 | 所有 LLM 输出必须先定义 Pydantic schema，再接 prompt |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Phase_Gated_Workflow_Pattern.md` | 阶段化流程与 gate | Compile Gate、Scenario Gate、Transcript Gate、Judge Gate 必须显式存在 |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Artifact_Centric_State_Pattern.md` | Artifact 即状态，可审计可 diff | `TaskUnderstanding`、`ScenarioSet`、`EpisodeTranscript`、`JudgeResult`、`Report` 都作为 artifact 存储 |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Perception_Action_Loop_Pattern.md` | observe -> act -> observe 闭环 | 多轮对话就是文本版 observe-act loop：观察模型回复，按隐藏目标推进用户发言 |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Agent_Persona.md` | Persona 配置驱动，不硬编码角色类 | 前端评测员画像应结构化存储并参与场景构建 |
| 本地知识库 | `E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Trace_Metamodel.md` | trace_id/span_id 统一观测模型 | 报告证据链必须可从 score item 回放到 turn 与 LLM 调用 |

### 33.4 新模块划分

建议新增或重构为以下模块。实现 Agent 应按模块拆分，不要在一个 service 里堆所有逻辑。

```text
outbound_eval/
  compiler/
    markdown_ast.py
    llm_task_compiler.py
    task_compiler_prompts.py
    compile_qa.py

  domain/
    schemas_markdown.py
    schemas_task.py
    schemas_judge.py
    schemas_persona.py
    schemas_scenario.py
    schemas_understanding.py

  llm/
    structured_client.py
    prompt_contracts.py
    visibility.py

  planner/
    scenario_policy.py
    scenario_planner_llm.py
    scenario_builder_llm.py
    scenario_qa.py

  simulator/
    user_simulator.py
    dialogue_manager.py
    visibility_filter.py

  evaluator/
    semantic_judge.py
    rule_guards.py
    finding_aggregator.py
    evidence_mapper.py

  reporting/
    evidence_report.py

  web/
    app.py
    static/app.js
    static/index.html
    static/styles.css
```

### 33.5 Markdown AST Parser

替换当前扁平 `section_splitter.py` 的主路径。旧函数可保留给兼容测试，但 `InstructionCompileService` 不应再依赖 `section_map()`。

参考实现：

```text
D:\Public Project\OpenSpec-main\src\core\parsers\markdown-parser.ts
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\markdown_ast.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_markdown.py
```

核心 schema：

```python
class MarkdownBullet(BaseModel):
    id: str
    text: str
    indent: int = 0
    ordered: bool = False
    line_no: int


class MarkdownNode(BaseModel):
    id: str
    heading: str
    normalized_heading: str
    level: int
    path: list[str]
    body: str
    bullets: list[MarkdownBullet] = []
    children: list["MarkdownNode"] = []
    start_line: int
    end_line: int
    raw_text: str


class MarkdownAst(BaseModel):
    root: MarkdownNode
    nodes: list[MarkdownNode]
    source_text: str
    parse_warnings: list[str] = []
```

关键实现约束：

1. 支持 `#` 到 `######`，不能只支持三级。
2. 必须像 OpenSpec 一样忽略 fenced code block 内的标题符号。
3. 没有任何标题时创建虚拟 root：`heading="Task Instruction"`、`level=0`。
4. 每个 node 必须保存 `start_line/end_line/raw_text`，后续 evidence 和 report 要引用它。
5. bullet 提取只做结构保留，不做业务理解；业务理解交给 `LLMTaskCompiler`。
6. parser 不允许识别“骑手/直播”等业务词。

### 33.6 LLMTaskCompiler

当前 `RuleBasedSpecExtractor` 应被降级为 fallback，默认主路径使用 LLM 编译。

参考实现：

```text
D:\Public Project\Dify-main\api\core\workflow\generator\runner.py
D:\Public Project\Dify-main\api\core\llm_generator\output_parser\structured_output.py
D:\Public Project\browser-use-main\browser_use\llm\openai\chat.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\llm_task_compiler.py
D:\OutboundEval-meituan\outbound_eval\compiler\task_compiler_prompts.py
D:\OutboundEval-meituan\outbound_eval\llm\structured_client.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_understanding.py
```

输出对象：

```python
class TaskUnderstanding(BaseModel):
    task_spec: TaskSpec
    judge_plan: JudgePlan
    risk_plan: RiskPlan
    source_map: dict[str, SourceRef]
    compiler_notes: list[str] = []
    compile_findings: list[CompileFinding] = []


class SourceRef(BaseModel):
    source_node_id: str
    heading_path: list[str]
    start_line: int
    end_line: int
    quote: str
```

需要扩展 `TaskSpec`：

```python
class KnowledgeFact(BaseModel):
    id: str
    text: str
    fact_type: Literal[
        "faq",
        "policy",
        "business_rule",
        "procedure",
        "definition",
        "constraint_detail",
    ]
    source_node_id: str
    source_text: str
    requirement_ids: list[str] = []


class TaskSpec(BaseModel):
    task_id: str
    task_name: str
    role: str
    objective: str
    opening_line: str = ""
    variables: list[TaskVariable] = []
    requirements: list[RequirementItem]
    flow_nodes: list[FlowNode] = []
    flow_edges: list[FlowEdge] = []
    branch_rules: list[BranchRule] = []
    knowledge_facts: list[KnowledgeFact] = []
    constraints: list[ConstraintRule] = []
    forbidden_behaviors: list[ForbiddenBehavior] = []
    termination_rules: list[TerminationRule] = []
    source_text: str
    source_map: dict[str, SourceRef] = {}
```

`FAQFact` 可以暂时保留兼容，但新流程必须写入 `knowledge_facts`。如果旧 checker 还依赖 `faq_facts`，normalizer 可把部分 `KnowledgeFact(fact_type="faq")` 映射为兼容 `FAQFact`。

LLM 编译 prompt 必须要求：

```text
你正在把评测员输入的外呼任务 Markdown 编译为评测规约。
不要假设业务领域固定。
不要新增原文没有的政策、金额、承诺、流程。
如果信息不足，输出 compile_findings，不要编造。
每个 requirement / knowledge_fact / judge_point 必须引用 source_node_id 和原文 quote。
输出严格 JSON。
```

编译器调用流程：

```python
class LLMTaskCompiler:
    async def compile(
        self,
        raw_instruction: str,
        markdown_ast: MarkdownAst,
        model_config: ModelConfig,
    ) -> TaskUnderstanding:
        draft = await structured_client.invoke(
            messages=build_compiler_messages(raw_instruction, markdown_ast),
            output_model=TaskUnderstandingDraft,
            model_config=model_config,
        )
        normalized = normalize_task_understanding(draft, markdown_ast)
        qa = CompileQAGate().validate(normalized)
        return normalized.model_copy(update={"compile_findings": qa.findings})
```

Compile QA 必须检查：

1. `TaskSpec.requirements` 非空。
2. 每个 requirement 有 `source_node_id/source_text`。
3. `JudgePlan.judge_points` 非空。
4. 关键 constraints、forbidden、termination 不得只留在自然语言摘要中，必须落结构字段。
5. `RiskPlan` 只记录风险识别，不阻塞场景未生成问题。

### 33.7 JudgePlan

当前系统没有真正的评分计划，导致 `SemanticJudge` 无法工作。必须新增 `JudgePlan`，由 `LLMTaskCompiler` 产出。

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\domain\schemas_judge.py
```

核心 schema：

```python
class JudgePoint(BaseModel):
    id: str
    dimension: Literal[
        "task_completion",
        "flow_following",
        "knowledge_correctness",
        "constraint_following",
        "exception_handling",
        "user_experience",
        "safety_compliance",
    ]
    criterion: str
    pass_criteria: str
    partial_criteria: str = ""
    fail_criteria: str
    evidence_required: bool = True
    weight: float = 1.0
    severity: Severity = Severity.MAJOR
    source_node_id: str
    source_text: str
    linked_requirement_ids: list[str] = []
    linked_knowledge_fact_ids: list[str] = []


class JudgePlan(BaseModel):
    task_id: str
    judge_points: list[JudgePoint]
    dimension_weights: dict[str, float]
    critical_failure_rules: list[CriticalFailureRule] = []
```

`JudgePoint` 的生成原则：

1. 原始任务中每个流程步骤至少一个 `flow_following` judge point。
2. 每个知识点至少一个 `knowledge_correctness` judge point。
3. 每条禁止行为至少一个 `constraint_following` 或 `safety_compliance` judge point。
4. 每条异常/挂断规则至少一个 `exception_handling` judge point。
5. 字数、禁词、挂断等可规则化项目仍要进入 JudgePlan，只是 `check_method="rule"`。

### 33.8 场景构建：ScenarioPlannerLLM + ScenarioBuilderLLM

当前 `CoveragePlanner.DEFAULT_SCENARIO_BLUEPRINTS` 是固定蓝图，不符合“评测员输入任意任务指令”的比赛定位。应拆成两段：

```text
ScenarioPlannerLLM: 根据 TaskSpec/JudgePlan 生成覆盖计划
ScenarioBuilderLLM: 根据覆盖计划 + 评测员画像生成完整 ScenarioSpec
```

参考实现：

```text
D:\Public Project\Dify-main\api\core\workflow\generator\prompts\planner_prompts.py
D:\Public Project\Dify-main\api\core\workflow\generator\prompts\builder_prompts.py
D:\Public Project\BMAD-METHOD-main\src\core-skills\bmad-review-edge-case-hunter\SKILL.md
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\planner\scenario_policy.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_planner_llm.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_builder_llm.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_qa.py
```

评测员画像输入：

```python
class EvaluatorPersonaInput(BaseModel):
    identity: str = ""
    relationship_to_task: str = ""
    motivation: str = ""
    attitude: str = ""
    communication_style: str = ""
    initial_focus: str = ""
    decision_rule: str = ""
    inconvenience_context: str = ""
    extra_notes: str = ""
```

场景计划：

```python
class ScenarioCoverageTarget(BaseModel):
    id: str
    title: str
    purpose: str
    linked_judge_point_ids: list[str]
    linked_requirement_ids: list[str]
    scenario_type: Literal[
        "main_flow",
        "branch",
        "knowledge_probe",
        "constraint_probe",
        "exception",
        "adversarial",
        "metamorphic",
    ]
    priority: Severity
```

完整场景：

```python
class LLMBuiltScenarioSpec(BaseModel):
    scenario_id: str
    task_id: str
    title: str
    scenario_type: str
    persona: EvaluatorPersonaInput
    user_goal: str
    hidden_goal: str
    initial_user_utterance: str
    dialogue_direction: list[str]
    expected_model_behavior: list[str]
    forbidden_behavior: list[str]
    stop_conditions: list[str]
    linked_judge_point_ids: list[str]
    covered_requirement_ids: list[str]
    max_turns: int = 10
    metadata: dict[str, Any] = {}
```

场景构建 prompt 的关键要求：

```text
你不是生成通用用户，而是生成能测试任务指令遵循的场景。
每个场景必须绑定 judge_point_ids。
必须融合评测员提供的 persona，但不得改变任务事实。
必须包含 initial_user_utterance 和 dialogue_direction。
不要使用固定业务动作名，如 ask_reward_rule、ask_refund。
如果任务没有某类内容，不要编造相应场景。
输出严格 JSON。
```

Scenario QA Gate 必须检查：

1. 每个 critical judge point 至少被一个场景覆盖。
2. 每个场景必须有 `user_goal`、`initial_user_utterance`、`linked_judge_point_ids`。
3. 场景不得把 `expected_model_behavior` 暴露给 target。
4. 如果 RiskPlan 产生 `RiskCoverageRequirement`，至少一个场景覆盖它；否则自动补生成，仍失败才 blocking。
5. 场景数量预算不足时优先保留：critical risk、主流程关键节点、知识点 probe、异常终止、禁承诺/禁编造。

### 33.9 用户模拟器重构

当前 `LLMUserSimulator` 仍以 action registry 为中心。重构后主路径应变成“场景驱动的角色扮演”，而不是“选择固定动作再渲染固定话术”。

参考实现：

```text
D:\Public Project\browser-use-main\browser_use\agent\views.py
D:\Public Project\browser-use-main\browser_use\agent\service.py
D:\Public Project\browser-use-main\browser_use\tools\registry\views.py
```

修改文件：

```text
D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py
D:\OutboundEval-meituan\outbound_eval\simulator\dialogue_manager.py
D:\OutboundEval-meituan\outbound_eval\simulator\visibility_filter.py
```

新的模拟输出：

```python
class UserSimulatorOutput(BaseModel):
    utterance: str
    intent: str
    state: str
    should_continue: bool
    covered_points: list[str] = []
    memory_update: str = ""
```

保留旧输出兼容：

```python
class LegacyActionOutput(BaseModel):
    action_name: str
    utterance: str
    end_call: bool = False
```

主流程不再要求 LLM 输出 `action_name`。`action_registry.py` 只作为：

1. 无模型配置时的 deterministic fallback。
2. 单元测试使用的固定路径回放。
3. 未来可选的动态动作空间实验。

用户模拟 prompt：

```text
你是电话中的模拟用户。
你只能根据 persona、scenario、对话历史自然回应。
你不能泄露隐藏测试目标、评分点、expected_model_behavior。
你的目标不是配合模型，而是按场景方向推进对话，触发指定测试点。
每次输出一句自然电话话术，尽量不超过 30 字，除非 persona 要求不同。
输出严格 JSON。
```

`DialogueManager` 负责：

1. 初始化首轮用户话术：优先使用 `scenario.initial_user_utterance`。
2. 每轮调用 target，再调用 user simulator。
3. 控制 `max_turns`。
4. 根据 `should_continue/stop_conditions/target goodbye/user hangup` 结束。
5. 检测重复、停滞、目标泄露。
6. 写入 `TurnEvent`、`SimulatorStateEvent`、`TraceEvent`。

### 33.10 被测对话 LLM 的可见信息边界

当前 `target_visible_context()` 的方向是正确的，应保留但加强为独立 `visibility_filter.py`。

目标模型只能看到：

```python
class TargetVisibleContext(BaseModel):
    system_prompt: str
    raw_instruction: str
    task_summary_for_target: str | None = None
    variables: dict[str, Any] = {}
    visible_history: list[TurnEvent]
```

不得看到：

```text
ScenarioSpec.hidden_goal
ScenarioSpec.expected_model_behavior
ScenarioSpec.forbidden_behavior
JudgePlan
RiskCoverageRequirement
EvaluatorPersonaInput.decision_rule
coverage target
score / finding / report metadata
```

建议新增测试：

```text
D:\OutboundEval-meituan\tests\test_visibility_boundaries.py
```

测试断言：

1. target context 不包含 `hidden_goal`。
2. target context 不包含 `judge_point`。
3. target context 不包含 `expected_model_behavior`。
4. simulator context 包含 scenario，但不能把 hidden goal 写入用户 utterance。

### 33.11 SemanticJudge 实现

当前 `D:\OutboundEval-meituan\outbound_eval\evaluator\semantic_judge.py` 是空实现，必须实现为 JudgePlan 驱动。

参考实现：

```text
D:\Public Project\browser-use-main\browser_use\agent\judge.py
D:\Public Project\ragflow-main\rag\prompts\citation_prompt.md
D:\Public Project\ragflow-main\rag\prompts\sufficiency_check.md
```

新增/修改文件：

```text
D:\OutboundEval-meituan\outbound_eval\evaluator\semantic_judge.py
D:\OutboundEval-meituan\outbound_eval\evaluator\evidence_mapper.py
D:\OutboundEval-meituan\outbound_eval\evaluator\finding_aggregator.py
```

LLM Judge 输入：

```python
class JudgeInput(BaseModel):
    task_spec: TaskSpec
    judge_plan: JudgePlan
    scenario: LLMBuiltScenarioSpec
    transcript: list[TurnEvent]
    rule_guard_results: list[JudgeEvent] = []
```

LLM Judge 输出：

```python
class JudgeItemResult(BaseModel):
    judge_point_id: str
    verdict: Literal["pass", "partial", "fail", "not_applicable"]
    score: float
    evidence_turn_ids: list[str]
    evidence_quote: str
    reason: str
    confidence: float = 0.8


class SemanticJudgeResult(BaseModel):
    scenario_id: str
    overall_summary: str
    item_results: list[JudgeItemResult]
    critical_failures: list[str] = []
```

LLM Judge prompt 必须强调：

```text
你只能根据 transcript 和 JudgePlan 判断。
每个 fail/partial 必须引用 evidence_turn_ids 和 evidence_quote。
不能因为对话听起来礼貌就通过，必须逐项对照 judge point。
如果 transcript 中没有证据，判 not_applicable 或 fail，不能脑补。
输出严格 JSON。
```

最终分数仍由 `ScoreAggregator` 确定性聚合，LLM 只产出 item verdict 和理由。严重错误封顶仍由第 32 节 Risk Guard / SeverityCap 机制执行。

### 33.12 Rule Guards 的新定位

规则 checker 不再承担语义理解。它只处理稳定、可确定的硬约束：

| Guard | 示例 | 实现位置 |
|---|---|---|
| 字数约束 | 每次回复 15-20 字以内 | `outbound_eval/evaluator/rule_guards.py` |
| 禁词 | 不说“好的、哈哈”等 | `rule_guards.py` |
| 挂断条件 | 用户说开车后是否继续纠缠 | `rule_guards.py` + `SemanticJudge` 交叉确认 |
| 信息泄露 | target 是否看到 hidden goal | `visibility_filter.py` + transcript QA |
| JSON/schema | LLM 输出是否符合 schema | `llm/structured_client.py` |

语义类判断交给 `SemanticJudge`：

```text
是否正确解释低延迟直播区别
是否基于任务说明解释奖励规则
是否正确处理非负责人分支
是否在用户坚持拒绝时安慰并结束
是否编造未给出的金额或政策
```

### 33.13 Risk Guard 与本次重构的关系

第 32 节的 `Risk Taxonomy + Guard Contract` 不废弃，但位置要变：

```text
LLMTaskCompiler
  -> TaskSpec + JudgePlan + RiskPlan
  -> RiskAuditor 检查 guard 完整性
  -> 生成 RiskCoverageRequirement
  -> ScenarioBuilderLLM 消费风险覆盖要求
  -> ScenarioQA 检查是否覆盖
  -> SemanticJudge / SeverityGuard 判分与封顶
```

因此实现 Agent 不能再做：

```text
检测到风险词 -> QA Gate 直接 blocking
```

而应做：

```text
检测到风险语义 -> 编译为 RiskPlan
  -> 检查 TaskSpec/JudgePlan 中是否有 grounding、forbidden、rubric、severity cap
  -> 有 guard 则 auto_guarded
  -> 场景构建阶段生成风险 probe
```

也就是说，Risk Guard 是“安全防线”，不是“任务模板识别器”。

### 33.14 Web API 重构

当前 `D:\OutboundEval-meituan\outbound_eval\web\app.py` 的 API 是：

```text
POST /api/compile
POST /api/qa
POST /api/plan
POST /api/run
POST /api/rejudge
```

建议新增并逐步替换：

```text
POST /api/tasks/parse-markdown
POST /api/tasks/compile-llm
POST /api/scenarios/build
POST /api/scenarios/qa
POST /api/runs/start
POST /api/evaluate/transcript
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/report
```

请求/响应：

```python
class CompileLLMRequest(BaseModel):
    instruction: str
    compiler_model_config: ModelConfig


class BuildScenariosRequest(BaseModel):
    task_understanding: TaskUnderstanding
    evaluator_persona: EvaluatorPersonaInput
    scenario_count: int = 8
    builder_model_config: ModelConfig


class StartRunRequest(BaseModel):
    instruction: str
    task_understanding: TaskUnderstanding
    scenarios: list[LLMBuiltScenarioSpec]
    target_model_config: ModelConfig
    simulator_model_config: ModelConfig
    judge_model_config: ModelConfig
    attempts: int = 1
    parallel: int = 1
```

前端必须新增：

1. Markdown 任务输入。
2. 评测员画像输入区：身份、关系、动机、态度、沟通风格、先问什么、决策规则。
3. “编译任务”结果面板：TaskSpec、JudgePlan、RiskPlan 摘要。
4. “生成场景”按钮。
5. 场景卡片：标题、用户画像、用户目标、初始话术、覆盖评分点、禁测点。
6. 场景可勾选/锁定后运行。
7. 对话回放和 evidence 高亮。

修改路径：

```text
D:\OutboundEval-meituan\outbound_eval\web\static\index.html
D:\OutboundEval-meituan\outbound_eval\web\static\app.js
D:\OutboundEval-meituan\outbound_eval\web\static\styles.css
```

### 33.15 Artifact 与存储

参考：

```text
D:\Public Project\ragflow-main\api\db\services\evaluation_service.py
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Artifact_Centric_State_Pattern.md
```

新增或确认以下 artifact：

| Artifact | 内容 | 存储表/目录 |
|---|---|---|
| `MarkdownAstArtifact` | 原始 Markdown 解析树 | `task_artifacts` |
| `TaskUnderstandingArtifact` | `TaskSpec + JudgePlan + RiskPlan` | `task_specs` 或新增 `task_understandings` |
| `ScenarioSetArtifact` | LLM 生成场景集合 | `scenario_definitions` |
| `EpisodeTranscriptArtifact` | 多轮对话 turn events | `episode_executions` |
| `JudgeResultArtifact` | item result、evidence、reason | `judge_events` |
| `ScoreEvidenceReport` | 汇总分、证据链、建议 | `report_artifacts` |
| `TraceArtifact` | LLM 调用、turn、judge span | `trace_events` 或 JSONL |

每个 artifact 必须有：

```python
artifact_id: str
task_id: str
run_id: str | None
artifact_type: str
version: str
created_at: datetime
source_hash: str
payload: dict
```

### 33.16 Trace 与 Evidence

参考：

```text
D:\Public Project\monocle-main\apptrace\src\README.md
D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\exporters\file_exporter.py
D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\instrumentation\common\span_handler.py
```

TraceEvent 建议：

```python
class TraceEvent(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    stage: Literal[
        "parse_markdown",
        "compile_task",
        "build_scenario",
        "simulate_user",
        "target_reply",
        "rule_guard",
        "semantic_judge",
        "score_aggregate",
        "report_generate",
    ]
    input_ref: str | None = None
    output_ref: str | None = None
    model_name: str | None = None
    token_usage: dict[str, int] = {}
    duration_ms: int | None = None
    status: Literal["ok", "error"] = "ok"
    error: str = ""
    metadata: dict[str, Any] = {}
```

EvidenceRef 建议：

```python
class EvidenceRef(BaseModel):
    episode_id: str
    turn_id: str
    role: Literal["user", "assistant", "system", "judge"]
    quote: str
    source_node_id: str | None = None
    judge_point_id: str | None = None
```

报告中所有失败项必须能落到：

```text
judge_point_id -> episode_id -> turn_id -> quote -> reason -> source_node_id
```

### 33.17 端到端运行时流程

建议最终主链路如下：

```python
async def run_generic_eval(request: StartRunRequest) -> RunResult:
    ast = MarkdownAstParser().parse(request.instruction)
    understanding = await LLMTaskCompiler().compile(
        raw_instruction=request.instruction,
        markdown_ast=ast,
        model_config=request.compiler_model_config,
    )
    compile_gate = CompileQAGate().validate(understanding)
    if compile_gate.has_blocking:
        return RunResult(stage="compile_qa", ok=False, findings=compile_gate.findings)

    scenario_set = await ScenarioBuilderService().build(
        understanding=understanding,
        evaluator_persona=request.evaluator_persona,
        model_config=request.builder_model_config,
    )
    scenario_gate = ScenarioQAGate().validate(understanding, scenario_set)
    if scenario_gate.can_autofill:
        scenario_set = await ScenarioBuilderService().autofill_missing(...)
    if scenario_gate.has_blocking:
        return RunResult(stage="scenario_qa", ok=False, findings=scenario_gate.findings)

    episodes = await BatchRunner().run_scenarios(
        instruction=request.instruction,
        understanding=understanding,
        scenarios=scenario_set.scenarios,
        target_model_config=request.target_model_config,
        simulator_model_config=request.simulator_model_config,
    )

    rule_events = RuleGuardPipeline().evaluate(understanding, episodes)
    semantic_events = await SemanticJudge().evaluate_many(
        understanding=understanding,
        scenarios=scenario_set.scenarios,
        episodes=episodes,
        model_config=request.judge_model_config,
    )
    findings = FindingAggregator().merge(rule_events, semantic_events)
    score = ScoreAggregator().aggregate(understanding.judge_plan, findings)
    report = ReportGenerator().build(understanding, scenario_set, episodes, findings, score)
    return RunResult(ok=True, report=report)
```

### 33.18 迁移优先级

P0：先修正产品主链路。

1. 新增 `MarkdownAstParser`，保留 nested headings、bullets、source line。
2. 新增 `StructuredLLMClient`，支持 Pydantic schema、json repair、三次重试。
3. 新增 `LLMTaskCompiler`，删除 `RuleBasedSpecExtractor._derive_task_name()` 的业务关键词逻辑。
4. 新增 `KnowledgeFact`、`JudgePlan`、`TaskUnderstanding`。
5. 新增 `/api/tasks/compile-llm`。
6. 新增 `EvaluatorPersonaInput` 和前端画像输入。
7. 新增 `ScenarioBuilderLLM` 和 `/api/scenarios/build`。
8. 前端展示场景卡片。

P1：让多轮模拟与评分真正通用。

1. 将 `CoveragePlanner.DEFAULT_SCENARIO_BLUEPRINTS` 降级为 fallback，不再默认主路径。
2. 将 `RiskScenarioFactory.RISK_ACTIONS` 降级为 fallback。
3. 改造 `LLMUserSimulator`，主输出为 `utterance/intent/state/should_continue`。
4. 新增 `DialogueManager` 管理 observe-act loop。
5. 实现 `SemanticJudge`，按 `JudgePlan` 输出 item verdict。
6. 新增 `FindingAggregator`，合并 rule、semantic、risk findings。
7. 报告改为 `任务点 -> 场景 -> 对话证据 -> 判分理由`。

P2：增强可信度与复测能力。

1. `RuleGuardPipeline` 只保留硬规则。
2. `RiskGuardCoverage` 接入 `RiskPlan`，不再从原文关键词直接主导流程。
3. 增加 badcase 复测：badcase 必须保存 `judge_point_id/scenario_id/evidence_turn_id`。
4. 增加 trace JSONL 降级导出，便于无 Postgres 环境调试。
5. 增加 task pack：把优秀任务样例保存为 `tasks/<slug>/task.md + persona.json + expected_scenarios.json`，只作回归测试，不作内置模板。

### 33.19 实现验收测试

建议新增测试文件：

```text
D:\OutboundEval-meituan\tests\test_markdown_ast_parser.py
D:\OutboundEval-meituan\tests\test_llm_task_compiler_contract.py
D:\OutboundEval-meituan\tests\test_scenario_builder_llm_contract.py
D:\OutboundEval-meituan\tests\test_visibility_boundaries.py
D:\OutboundEval-meituan\tests\test_semantic_judge_contract.py
D:\OutboundEval-meituan\tests\test_generic_eval_pipeline.py
```

关键测试用例：

| 测试名 | 目标 |
|---|---|
| `test_markdown_ast_preserves_nested_steps` | `# Conversation Flow / ## Step 3 / ### 3.1` 不丢层级 |
| `test_markdown_ast_ignores_headings_in_code_fence` | code block 内 `#` 不被识别为标题 |
| `test_llm_task_compiler_has_no_business_keyword_task_name` | 输入不含飞毛腿/直播也能编译；不再靠关键词命名 |
| `test_bullet_knowledge_becomes_knowledge_fact` | bullet 知识点生成 `KnowledgeFact` |
| `test_judge_plan_links_every_critical_requirement` | critical requirement 有 judge point |
| `test_scenario_builder_uses_evaluator_persona` | 画像中的身份/动机/态度进入场景 |
| `test_scenario_builder_links_judge_points` | 每个场景都有 `linked_judge_point_ids` |
| `test_user_simulator_does_not_reveal_hidden_goal` | 用户话术不泄露隐藏目标 |
| `test_target_context_excludes_scenario_and_judge_plan` | target 看不到场景和评分计划 |
| `test_semantic_judge_requires_evidence_for_fail` | fail/partial 必须有 turn evidence |
| `test_report_links_score_to_turn_and_source_node` | 报告能从分数追溯到 turn 和原文节点 |

### 33.20 给实现 Agent 的明确禁令

实现本节时，下面几类改法都视为偏离方向：

1. 不要再新增“飞毛腿”“课程直播”“奖励”等业务关键词分支。
2. 不要让用户模拟器依赖固定 action name 才能工作。
3. 不要让场景规划只输出 `happy_path/refusal/driving/boundary` 这类通用模板。
4. 不要让 LLM judge 只输出总分或自然语言总结。
5. 不要把场景隐藏目标、judge point、expected behavior 暴露给被测模型。
6. 不要在 QA Gate 阶段因为“场景还没覆盖”阻塞；场景覆盖属于 Scenario QA。
7. 不要把 Risk Guard 当作任务理解主入口；它只能校验防护结构和生成覆盖要求。

### 33.21 最终重构后的产品表达

这个项目最终应被描述为：

```text
一个通用外呼任务指令评测操作台。

评测员输入任意半结构化 Markdown 任务说明和模拟用户画像；
系统先用 LLM 将任务说明编译成统一 TaskSpec、JudgePlan、RiskPlan；
再由场景构建 LLM 针对任务关键点生成多组测试场景；
用户模拟 LLM 根据画像和场景隐藏目标推进多轮对话；
被测对话 LLM 只看到任务指令和可见对话历史；
评估 LLM 按 JudgePlan 对完整 transcript 逐项打分；
规则 Guard 和风险 Guard 负责硬约束、严重错误与封顶；
最终报告把每个分数追溯到场景、对话轮次和任务原文。
```

这比“内置几个外呼任务模板”更符合官方定位，也更能体现大模型能力：不是把规则写死，而是让系统把任意任务指令转化为可执行、可模拟、可评分、可解释的评测程序。

### 33.22 可直接派发给实现 Agent 的模块实现包

本节把重构拆成可以分配给不同实现 Agent 的工程包。每个包都包含：目标、必须修改/新增的本项目文件、必须阅读的本地参考源码、核心函数边界、输入输出契约、验收条件。实现 Agent 即使看不到当前知识库，也可以按这里的路径打开参考代码。

#### Agent A：MarkdownAstParser 与 SourceMap

目标：把任意半结构化 Markdown 任务说明解析成层级 AST，不做业务理解，只保留结构、原文、行号、bullet 和 source id。

当前待替换文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\section_splitter.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\markdown_ast.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_markdown.py
```

必须阅读参考源码：

```text
D:\Public Project\OpenSpec-main\src\core\parsers\markdown-parser.ts
D:\Public Project\OpenSpec-main\src\core\parsers\requirement-blocks.ts
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Static_Parser.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Artifact_Centric_State_Pattern.md
```

参考约束：

- `markdown-parser.ts` 中 `buildCodeFenceMask()` 和 `parseSections()` 是核心参考：先为 fenced code block 建 mask，再用 stack 生成 `children`。
- `requirement-blocks.ts` 的价值是保存 raw block 和 header/body，不要只存归一化文本。
- 本项目 parser 禁止出现“骑手、飞毛腿、直播、课程”等业务词判断。

核心接口：

```python
class MarkdownAstParser:
    def parse(self, source_text: str) -> MarkdownAst:
        ...

    def flatten(self, ast: MarkdownAst) -> list[MarkdownNode]:
        ...
```

关键 schema：

```python
class MarkdownNode(BaseModel):
    id: str
    heading: str
    normalized_heading: str
    level: int
    path: list[str]
    body: str
    bullets: list[MarkdownBullet]
    children: list["MarkdownNode"]
    start_line: int
    end_line: int
    raw_text: str

class SourceRef(BaseModel):
    source_node_id: str
    heading_path: list[str]
    start_line: int
    end_line: int
    quote: str
```

验收测试：

```text
D:\OutboundEval-meituan\tests\test_markdown_ast_parser.py
```

必须覆盖：

1. 支持 `#` 到 `######`。
2. code fence 内的 `# Heading` 不成为节点。
3. 无标题输入产生虚拟 root。
4. `# Flow / ## Step 3 / ### 3.1` 保留层级。
5. 每个节点有稳定 `id/start_line/end_line/raw_text`。

#### Agent B：StructuredLLMClient 与模型适配层

目标：所有 LLM 编译、场景构建、用户模拟、语义评分都通过统一 structured output 客户端调用，不允许各模块直接拼 OpenAI SDK。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\adapters\openai_compatible.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_model.py
D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\llm\structured_client.py
D:\OutboundEval-meituan\outbound_eval\llm\prompt_contracts.py
D:\OutboundEval-meituan\outbound_eval\llm\visibility.py
```

必须阅读参考源码：

```text
D:\Public Project\Dify-main\api\core\llm_generator\output_parser\structured_output.py
D:\Public Project\Dify-main\api\core\workflow\node_runtime.py
D:\Public Project\browser-use-main\browser_use\llm\openai\chat.py
D:\Public Project\AutoGPT-master\autogpt_platform\backend\backend\util\openai_responses.py
D:\Public Project\AutoGPT-master\autogpt_platform\backend\backend\util\retry.py
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\OpenAI_Compatible_Facade_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Schema_First_Block_Pattern.md
```

参考约束：

- Dify `structured_output.py` 体现 JSON Schema / JSON mode / prompt-based schema / repair 的分层。
- Dify `node_runtime.py` 体现运行节点不直接感知模型 SDK。
- BrowserUse `chat.py` 的 `output_format=PydanticModel` 是 Pydantic schema-first 调用参考。
- AutoGPT `retry.py` 与 `openai_responses.py` 可参考模型调用失败重试和响应抽象，不照搬依赖。

核心接口：

```python
class StructuredLLMClient:
    async def invoke_json(
        self,
        *,
        model_config: ModelConfig,
        messages: list[dict[str, str]],
        output_model: type[BaseModel],
        stage: str,
        temperature: float = 0.2,
        max_retries: int = 3,
    ) -> StructuredLLMResult:
        ...

class StructuredLLMResult(BaseModel):
    parsed: BaseModel
    raw_text: str
    repaired: bool = False
    retry_count: int = 0
    trace_id: str | None = None
    warnings: list[str] = []
```

硬约束：

1. 所有 LLM JSON 输出必须 `Pydantic.model_validate()`。
2. JSON repair 只能修格式，不能补业务语义。
3. 模型调用必须写 `TraceEvent`：stage、model、duration、status、tokens、raw_text 摘要。
4. compiler/simulator/judge 不允许导入 `openai.AsyncOpenAI`。

#### Agent C：LLMTaskCompiler 与统一 TaskUnderstanding

目标：替换规则主导的 `RuleBasedSpecExtractor`，用 Markdown AST + LLM 生成统一任务理解层：`TaskSpec + JudgePlan + RiskPlan + SourceMap`。

当前待降级文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\llm_spec_extractor.py
D:\OutboundEval-meituan\outbound_eval\compiler\compile_service.py
D:\OutboundEval-meituan\outbound_eval\compiler\spec_normalizer.py
D:\OutboundEval-meituan\outbound_eval\compiler\spec_validator.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_task.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\compiler\llm_task_compiler.py
D:\OutboundEval-meituan\outbound_eval\compiler\task_compiler_prompts.py
D:\OutboundEval-meituan\outbound_eval\compiler\compile_qa.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_understanding.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_judge.py
```

必须阅读参考源码：

```text
D:\Public Project\Dify-main\api\core\workflow\generator\runner.py
D:\Public Project\Dify-main\api\core\workflow\generator\types.py
D:\Public Project\OpenSpec-main\src\core\validation\validator.ts
D:\Public Project\OpenSpec-main\src\core\schemas\base.schema.ts
D:\Public Project\ragflow-main\rag\prompts\sufficiency_check.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Phase_Gated_Workflow_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Instruction_As_Code_Pattern.md
```

参考约束：

- Dify `runner.py` 的 Planner -> Builder -> Postprocess -> Validation 可迁移为 Analyze -> Compile -> Normalize -> QA。
- OpenSpec `validator.ts` 的关键不是 TS 代码，而是“parse 后显式 validation report”。
- RAGFlow `sufficiency_check.md` 可迁移为 `CompileSufficiencyCheck`：输出 `is_sufficient/reasoning/missing_information`。

核心接口：

```python
class LLMTaskCompiler:
    async def compile(
        self,
        *,
        raw_markdown: str,
        markdown_ast: MarkdownAst,
        model_config: ModelConfig,
        evaluator_config: EvaluatorCompileConfig | None = None,
    ) -> TaskUnderstanding:
        ...
```

关键输出：

```python
class TaskUnderstanding(BaseModel):
    task_spec: TaskSpec
    judge_plan: JudgePlan
    risk_plan: RiskPlan
    source_map: dict[str, SourceRef]
    compiler_notes: list[str] = []
    compile_findings: list[CompileFinding] = []
```

`TaskSpec` 必须升级：

```python
class KnowledgeFact(BaseModel):
    id: str
    fact_type: Literal["faq", "policy", "definition", "procedure", "constraint_context", "other"]
    statement: str
    question_patterns: list[str] = []
    answer: str | None = None
    source_ref_id: str
    linked_requirement_ids: list[str]

class RequirementItem(BaseModel):
    id: str
    name: str
    category: RequirementCategory
    source_ref_id: str
    source_text: str
    check_method: CheckMethod
    severity: Severity
    tags: list[str]
```

编译 prompt 必须要求：

1. 不假设固定标题，只利用 Markdown 层级和语义。
2. 提取 Role、Objective、Opening、Flow、Branch、Knowledge、Constraint、Forbidden、Termination、Variables。
3. 对每个 critical/major requirement 生成 judge point。
4. 每个对象必须引用 `source_ref_id`。
5. 不得把没有依据的业务规则补成事实；不确定时生成 `CompileFinding`。

验收条件：

1. 删除 `_derive_task_name()` 中业务关键词分支，或仅放在 fallback 且默认不走。
2. 输入完全陌生行业任务也能生成 `TaskSpec/JudgePlan`。
3. bullet 知识点不再被强塞进 Q/A，而是成为 `KnowledgeFact`。
4. `TaskUnderstanding` 是三类 LLM 的唯一结构化理解源。

#### Agent D：JudgePlan、RiskPlan 与 QA Gate

目标：把评分标准从规则 checker 里抽出来，编译阶段生成 `JudgePlan`；Risk Guard 只作为防护完整性校验与覆盖要求，不再主导任务建模。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\domain\schemas_judge.py
D:\OutboundEval-meituan\outbound_eval\spec_qa\service.py
D:\OutboundEval-meituan\outbound_eval\spec_qa\risk_auditor.py
D:\OutboundEval-meituan\outbound_eval\spec_qa\guard_contract.py
D:\OutboundEval-meituan\outbound_eval\spec_qa\risk_taxonomy.yaml
D:\OutboundEval-meituan\outbound_eval\planner\coverage_qa.py
```

必须阅读参考源码：

```text
D:\Public Project\BMAD-METHOD-main\src\bmm-skills\4-implementation\bmad-code-review\steps\step-03-triage.md
D:\Public Project\BMAD-METHOD-main\src\core-skills\bmad-review-edge-case-hunter\SKILL.md
D:\Public Project\OpenSpec-main\src\core\validation\validator.ts
D:\Public Project\ragflow-main\rag\prompts\sufficiency_check.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Policy_Guard.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Human_Review_Gate.md
```

核心 schema：

```python
class JudgePoint(BaseModel):
    id: str
    dimension: Literal[
        "task_completion", "flow_following", "knowledge_correctness",
        "constraint_following", "exception_handling", "user_experience", "safety"
    ]
    criterion: str
    pass_criteria: str
    partial_criteria: str
    fail_criteria: str
    severity: Severity
    weight: float
    linked_requirement_ids: list[str]
    linked_source_ref_ids: list[str]
    evidence_required: bool = True
    evaluator: Literal["rule", "llm", "hybrid"]

class JudgePlan(BaseModel):
    task_id: str
    judge_points: list[JudgePoint]
    severity_caps: list[SeverityCap]
    aggregation_policy: AggregationPolicy

class RiskPlan(BaseModel):
    detected_risks: list[DetectedRisk]
    guard_statuses: list[RiskGuardStatus]
    coverage_requirements: list[RiskCoverageRequirement]
```

QA Gate 分层：

```text
CompileQAGate:
  输入 TaskUnderstanding
  检查 source_ref、requirement/judge/risk 结构完整性
  输出 CompileFinding
  不检查场景是否已经生成

ScenarioQAGate:
  输入 TaskUnderstanding + ScenarioSet
  检查 JudgePoint / RiskCoverageRequirement 是否被 Scenario 覆盖
  缺失时优先自动补场景
```

硬约束：

1. `blocking_findings` 只允许 unresolved critical 且缺少必要结构的项。
2. 已被 guard 覆盖的风险只能进入报告，不得阻塞。
3. `coverage_requirement` 在 Compile Gate 只生成，不验证完成。
4. 多来源 finding 必须归一化、去重、带稳定 code。

#### Agent E：ScenarioPlannerLLM 与 ScenarioBuilderLLM

目标：把当前固定蓝图 `CoveragePlanner.DEFAULT_SCENARIO_BLUEPRINTS` 降级为 fallback。主流程由 LLM 根据 `TaskSpec/JudgePlan/RiskPlan/评测员画像` 生成真实场景。

当前待重构文件：

```text
D:\OutboundEval-meituan\outbound_eval\planner\coverage_planner.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_generator.py
D:\OutboundEval-meituan\outbound_eval\planner\risk_scenario_factory.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_scenario.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\planner\scenario_policy.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_planner_llm.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_builder_llm.py
D:\OutboundEval-meituan\outbound_eval\planner\scenario_qa.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_persona.py
```

必须阅读参考源码：

```text
D:\Public Project\Dify-main\api\core\workflow\generator\prompts\planner_prompts.py
D:\Public Project\Dify-main\api\core\workflow\generator\prompts\builder_prompts.py
D:\Public Project\Dify-main\api\core\workflow\generator\runner.py
D:\Public Project\BMAD-METHOD-main\src\core-skills\bmad-review-edge-case-hunter\SKILL.md
D:\Public Project\superpowers-main\skills\writing-plans\SKILL.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Input_Simulator.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Agent_Persona.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Vertical_Slice_Task_Decomposition_Pattern.md
```

两段式生成：

```python
class ScenarioPlannerLLM:
    async def plan(self, understanding: TaskUnderstanding, persona: EvaluatorPersonaInput, budget: int) -> ScenarioPlan:
        ...

class ScenarioBuilderLLM:
    async def build(self, understanding: TaskUnderstanding, scenario_plan: ScenarioPlan) -> ScenarioSet:
        ...
```

第一段 `ScenarioPlan` 只输出覆盖意图：

```python
class ScenarioPlanItem(BaseModel):
    id: str
    title: str
    scenario_type: ScenarioType
    coverage_intent: str
    linked_judge_point_ids: list[str]
    linked_requirement_ids: list[str]
    linked_risk_coverage_requirement_ids: list[str] = []
    persona_focus: str
    priority: Severity
```

第二段 `ScenarioSpec` 输出完整场景：

```python
class ScenarioSpec(BaseModel):
    scenario_id: str
    title: str
    scenario_type: ScenarioType
    persona: PersonaSpec
    hidden_user_goal: str
    initial_user_utterance: str
    dialogue_direction: list[str]
    expected_model_behavior: list[str]
    forbidden_behavior: list[str]
    stop_conditions: list[str]
    linked_judge_point_ids: list[str]
    covered_requirement_ids: list[str]
    metadata: dict[str, Any]
```

场景生成 prompt 必须要求：

1. 场景要贴合当前任务，不允许只输出 `happy path / busy / refusal`。
2. 评测员画像必须进入 persona 和 hidden goal。
3. 每个 critical judge point 至少一个场景覆盖。
4. 每个 risk coverage requirement 至少一个场景覆盖。
5. 场景要能触发分支、追问、打断、拒绝、边界提问，但具体内容来自任务，不来自内置模板。
6. 每个场景必须能在前端解释：为什么测、测什么、预期模型怎么做。

验收条件：

1. `CoveragePlanner.plan()` 默认调用 LLM scenario builder；固定蓝图只在 `offline_fallback=True` 时使用。
2. `RiskScenarioFactory.RISK_ACTIONS` 不再是主路径。
3. 生成“大学生、报名飞毛腿玩玩、不缺钱、态度强硬、先问报酬”画像时，场景必须体现这些字段。
4. 生成课程直播任务时，场景必须包含“低延迟价格追问/优惠承诺风险/配置不可见”等来自任务的真实场景，而不是 generic boundary question。

#### Agent F：UserSimulatorLLM 与 DialogueManager

目标：用户模拟器只负责扮演和推进，不负责设计场景；它输出自然话术、状态和是否继续，不再强制选择固定业务 action。

当前待重构文件：

```text
D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py
D:\OutboundEval-meituan\outbound_eval\simulator\action_registry.py
D:\OutboundEval-meituan\outbound_eval\runner\episode_runner.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\simulator\dialogue_manager.py
D:\OutboundEval-meituan\outbound_eval\simulator\visibility_filter.py
```

必须阅读参考源码：

```text
D:\Public Project\browser-use-main\browser_use\agent\views.py
D:\Public Project\browser-use-main\browser_use\agent\service.py
D:\Public Project\browser-use-main\browser_use\tools\registry\views.py
D:\Public Project\browser-use-main\browser_use\tools\registry\service.py
D:\Public Project\langgraph-main\libs\langgraph\langgraph\graph\state.py
D:\Public Project\langgraph-main\libs\langgraph\langgraph\pregel\_checkpoint.py
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Perception_Action_Loop_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Post_Turn_Review_Pattern.md
```

参考约束：

- BrowserUse `AgentOutput` 的 `evaluation_previous_goal/memory/next_goal/action` 可改造成文本对话版：`state/memory_update/next_intent/utterance`。
- BrowserUse `ActionLoopDetector` 可迁移为对话卡住检测：用户重复、模型重复、无新信息、超轮次。
- LangGraph `StateGraph` 和 checkpoint 思路用于 episode 状态机，而不是必须引入 LangGraph 依赖。

核心接口：

```python
class UserSimulatorLLM:
    async def next_turn(
        self,
        *,
        task_view: SimulatorTaskView,
        scenario: ScenarioSpec,
        persona: PersonaSpec,
        history: list[TurnEvent],
        memory: SimulatorMemory,
        model_config: ModelConfig,
    ) -> UserSimulatorOutput:
        ...

class UserSimulatorOutput(BaseModel):
    utterance: str
    intent: str
    state: str
    memory_update: str = ""
    should_continue: bool
    covered_judge_point_ids: list[str] = []
    covered_requirement_ids: list[str] = []
    stop_reason: str | None = None
```

`DialogueManager` 状态机：

```text
start
  -> simulator_user_turn
  -> target_model_turn
  -> post_turn_review
  -> stop? else simulator_user_turn
  -> transcript_complete
```

硬约束：

1. 用户模拟 LLM 不能看到完整 `JudgePlan.fail_criteria`，只能看到当前场景中允许的 hidden goal 和 expected behavior 摘要。
2. 用户话术不允许泄露“我在测试你是否...”。
3. fallback `_render()` 可以保留，但只用于无模型或测试，不得作为默认主路径。
4. `action_registry.py` 中业务动作名必须降级为 fallback 或迁移到抽象 intent，不再污染主 prompt。

#### Agent G：TargetDialogueLLM 可见边界与被测模型适配

目标：被测模型只看到外呼任务指令、变量、可见对话历史；看不到场景、用户画像隐藏目标、judge point、risk coverage。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\simulator\user_simulator.py
D:\OutboundEval-meituan\outbound_eval\adapters\openai_compatible.py
D:\OutboundEval-meituan\outbound_eval\runner\episode_runner.py
```

新增或调整文件：

```text
D:\OutboundEval-meituan\outbound_eval\llm\visibility.py
D:\OutboundEval-meituan\outbound_eval\simulator\visibility_filter.py
```

必须阅读参考源码：

```text
D:\Public Project\Dify-main\api\core\workflow\workflow_entry.py
D:\Public Project\ragflow-main\api\apps\restful_apis\agent_api.py
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Agent_Scope.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Fail_Closed_Security_Pattern.md
```

核心接口：

```python
class VisibilityFilter:
    def target_messages(
        self,
        *,
        raw_instruction: str,
        task_summary: str | None,
        variables: dict[str, Any],
        history: list[TurnEvent],
    ) -> list[dict[str, str]]:
        ...

    def simulator_view(self, understanding: TaskUnderstanding, scenario: ScenarioSpec) -> SimulatorTaskView:
        ...

    def judge_view(self, understanding: TaskUnderstanding, scenario: ScenarioSpec, transcript: EpisodeExecution) -> JudgeTaskView:
        ...
```

验收测试：

```text
D:\OutboundEval-meituan\tests\test_visibility_boundaries.py
```

必须断言：

1. target messages 不含 `hidden_user_goal`。
2. target messages 不含 `judge_point`、`expected_model_behavior`、`forbidden_behavior`。
3. target messages 不含 `risk_coverage_requirement`。
4. simulator view 不含最终评分权重和封顶策略。
5. judge view 含 transcript、scenario、judge plan、source map。

#### Agent H：SemanticJudge、EvidenceMapper 与 FindingAggregator

目标：实现真正的 LLM 语义评分。LLM judge 逐项评估 `JudgePoint`，必须输出 verdict、score、evidence turn、reason。规则 checker 只做硬约束。

当前待重构文件：

```text
D:\OutboundEval-meituan\outbound_eval\evaluator\semantic_judge.py
D:\OutboundEval-meituan\outbound_eval\evaluator\ensemble.py
D:\OutboundEval-meituan\outbound_eval\evaluator\rule_checker.py
D:\OutboundEval-meituan\outbound_eval\evaluator\constraint_checker.py
D:\OutboundEval-meituan\outbound_eval\evaluator\flow_checker.py
D:\OutboundEval-meituan\outbound_eval\evaluator\knowledge_checker.py
D:\OutboundEval-meituan\outbound_eval\scoring\aggregator.py
```

新增文件：

```text
D:\OutboundEval-meituan\outbound_eval\evaluator\rule_guards.py
D:\OutboundEval-meituan\outbound_eval\evaluator\evidence_mapper.py
D:\OutboundEval-meituan\outbound_eval\evaluator\finding_aggregator.py
D:\OutboundEval-meituan\outbound_eval\evaluator\semantic_judge_prompts.py
```

必须阅读参考源码：

```text
D:\Public Project\browser-use-main\browser_use\agent\judge.py
D:\Public Project\ragflow-main\rag\prompts\citation_prompt.md
D:\Public Project\BMAD-METHOD-main\src\bmm-skills\4-implementation\bmad-code-review\steps\step-03-triage.md
D:\Public Project\Understand-Anything-main
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Reviewer_Fanout_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Goal_Driven_Verification_Pattern.md
```

核心输出：

```python
class JudgePointResult(BaseModel):
    judge_point_id: str
    verdict: Literal["pass", "partial", "fail", "not_applicable"]
    score: float
    evidence_turn_ids: list[str]
    evidence_quotes: list[str]
    reason: str
    confidence: float
    suggested_fix: str = ""

class SemanticJudgeResult(BaseModel):
    scenario_id: str
    episode_id: str
    item_results: list[JudgePointResult]
    summary: str
    critical_failures: list[str] = []
```

LLM judge prompt 必须包含：

1. `task_summary`
2. `scenario`
3. `judge_points`
4. `source_refs`
5. `transcript`
6. 严格 JSON schema

LLM judge prompt 不允许：

1. 要求“给总体印象分”。
2. 没有证据就判 fail。
3. 使用任务原文之外的业务常识扩展政策。

Rule Guards 新定位：

| Guard | 负责内容 |
|---|---|
| `LengthGuard` | 字数/轮次/超时 |
| `ForbiddenPhraseGuard` | 禁词、禁止开场词 |
| `VisibilityLeakGuard` | 是否泄露隐藏测试目标 |
| `TerminationGuard` | 用户开车/坚持拒绝时是否终止 |
| `SeverityCapGuard` | 严重错误封顶 |

`FindingAggregator` 参考 BMAD triage：

```text
rule finding + semantic finding + risk finding
  -> normalize
  -> dedupe by judge_point_id/evidence_turn/source
  -> classify: blocking / scoring / report_only / dismissed
```

验收条件：

1. `SemanticJudge.evaluate()` 不再返回空列表。
2. fail/partial 必须有 `evidence_turn_ids`。
3. rule checker 不再试图用关键词覆盖率判断复杂语义。
4. 同一违规不重复扣多次，除非来自不同 judge point。

#### Agent I：Runtime、EpisodeRunner、Trace 与 Artifact Store

目标：把端到端评测运行做成可追踪、可复跑的阶段化 workflow。每个阶段产物作为 artifact 存储，episode 是状态机，不是一次性函数里的临时变量。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\runner\episode_runner.py
D:\OutboundEval-meituan\outbound_eval\runner\rejudge.py
D:\OutboundEval-meituan\outbound_eval\trace\store.py
D:\OutboundEval-meituan\outbound_eval\trace\postgres_store.py
D:\OutboundEval-meituan\outbound_eval\storage\postgres_repository.py
D:\OutboundEval-meituan\outbound_eval\storage\sqlite_repository.py
```

必须阅读参考源码：

```text
D:\Public Project\langgraph-main\libs\langgraph\langgraph\graph\state.py
D:\Public Project\langgraph-main\libs\langgraph\langgraph\pregel\_checkpoint.py
D:\Public Project\harness-main\job\scheduler.go
D:\Public Project\harness-main\job\executor.go
D:\Public Project\harness-main\events\stream.go
D:\Public Project\harness-main\livelog\stream.go
D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\exporters\file_exporter.py
D:\Public Project\monocle-main\apptrace\src\monocle_apptrace\instrumentation\common\span_handler.py
D:\Public Project\agentmemory-main\src\functions\observe.ts
D:\Public Project\agentmemory-main\src\functions\replay.ts
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Trace_Metamodel.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Queue_Based_Execution_Pattern.md
```

参考约束：

- LangGraph：状态图和 checkpoint 思路用于 episode 状态恢复。
- Harness：`job/executor.go` 的 handler registry + progress reporter 可迁移为 stage handler 注册和进度上报。
- Monocle：trace/span 结构用于 LLM 调用、turn、judge item。
- agentmemory：`observe.ts/replay.ts` 用于 badcase 复盘和 run replay 的文件/事件结构参考。

运行阶段：

```text
compile_task
  -> compile_qa
  -> build_scenarios
  -> scenario_qa
  -> run_episodes
  -> semantic_judge
  -> rule_guards
  -> aggregate_score
  -> generate_report
```

核心接口：

```python
class EvaluationRuntime:
    async def run(self, request: EvaluationRunRequest) -> EvaluationRunResult:
        ...

class StageHandler(Protocol):
    async def handle(self, state: EvaluationRunState, reporter: ProgressReporter) -> EvaluationRunState:
        ...

class ArtifactStore:
    def put_artifact(self, run_id: str, artifact_type: str, artifact_id: str, payload: BaseModel | dict) -> None:
        ...

    def get_artifact(self, artifact_type: str, artifact_id: str) -> dict | None:
        ...
```

必须存储 artifact：

```text
TaskUnderstanding
ScenarioPlan
ScenarioSet
EpisodeTranscript
SemanticJudgeResult
RuleGuardResult
ScoreAggregate
ReportArtifact
TraceEvents
```

验收条件：

1. 任一 run 可按 `run_id` 查看阶段状态和产物。
2. 单个 episode 可 replay：输入同一 scenario 和模型配置可复测。
3. 每个 judge result 可回溯到 `episode_id/turn_id/source_ref_id`。
4. 无 Postgres 时 JSONL artifact store 可用，正式交付使用 Postgres。

#### Agent J：Web 操作台与 API

目标：前端从“输入任务后一键跑”升级为评测操作台：任务输入、用户画像、任务理解预览、场景卡片、运行监控、评估报告。

当前待改文件：

```text
D:\OutboundEval-meituan\outbound_eval\web\app.py
D:\OutboundEval-meituan\outbound_eval\web\static\index.html
D:\OutboundEval-meituan\outbound_eval\web\static\app.js
D:\OutboundEval-meituan\outbound_eval\web\static\styles.css
```

必须阅读参考源码：

```text
D:\Public Project\ragflow-main\api\apps\restful_apis\agent_api.py
D:\Public Project\harness-main\web\src\hooks\useEventListener.ts
D:\Public Project\harness-main\events\stream.go
D:\Public Project\agentmemory-main\src\viewer\server.ts
D:\Public Project\superpowers-main\skills\brainstorming\scripts\server.cjs
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Progressive_Disclosure_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Event_Driven_State_Sync_Pattern.md
```

新增 API：

```text
POST /api/task/understand
POST /api/scenarios/build
POST /api/scenarios/qa
POST /api/run/start
GET  /api/run/{run_id}/status
GET  /api/run/{run_id}/events
GET  /api/run/{run_id}/artifacts
POST /api/rejudge
```

请求对象：

```python
class TaskUnderstandRequest(BaseModel):
    instruction: str
    compiler_model_config: ModelConfig

class ScenarioBuildRequest(BaseModel):
    task_understanding: dict[str, Any]
    evaluator_persona: EvaluatorPersonaInput
    scenario_count: int = 8
    builder_model_config: ModelConfig

class EvaluatorPersonaInput(BaseModel):
    identity: str = ""
    relationship_to_task: str = ""
    motivation: str = ""
    attitude: str = ""
    communication_style: str = ""
    initial_focus: str = ""
    decision_rule: str = ""
    extra_notes: str = ""
```

前端视图：

| 页面区域 | 功能 |
|---|---|
| 任务输入 | Markdown 编辑区，不预设飞毛腿模板作为唯一入口 |
| 用户画像 | 身份、关系、动机、态度、沟通风格、决策规则 |
| 任务理解预览 | Role / Objective / Flow / Knowledge / Constraints / JudgePlan |
| 场景卡片 | 展示 title、persona、user_goal、initial utterance、linked judge points |
| 运行监控 | 按 episode 展示 transcript 和状态 |
| 报告 | 总分、维度分、失败项、证据 turn、原文 source |

硬约束：

1. 不做营销页；第一屏就是评测操作台。
2. 场景卡片必须能被评测员锁定、禁用或重新生成。
3. 被测模型配置、编译模型配置、评估模型配置可以分开。
4. 页面不能把 hidden goal 暴露到“被测模型上下文预览”里。

#### Agent K：Report、Badcase、Golden Set

目标：报告从“结果摘要”升级为“评测审计文件”。badcase 可以沉淀为复测用例，golden set 支持人工标注对比。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\reporting\generator.py
D:\OutboundEval-meituan\outbound_eval\badcase.py
D:\OutboundEval-meituan\outbound_eval\golden.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_report.py
D:\OutboundEval-meituan\outbound_eval\domain\schemas_score.py
```

必须阅读参考源码：

```text
D:\Public Project\ragflow-main\rag\prompts\citation_prompt.md
D:\Public Project\ragflow-main\api\db\services\evaluation_service.py
D:\Public Project\agentmemory-main\src\functions\replay.ts
D:\Public Project\agentmemory-main\src\viewer\server.ts
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Artifact_Centric_State_Pattern.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Reference_Resolver.md
```

报告结构：

```python
class EvidenceReportItem(BaseModel):
    judge_point_id: str
    result: str
    score_delta: float
    evidence_turn_ids: list[str]
    evidence_quotes: list[str]
    source_ref_ids: list[str]
    reason: str
    suggested_fix: str

class ReportArtifact(BaseModel):
    run_id: str
    task_summary: str
    scenario_coverage_summary: ScenarioCoverageSummary
    score: ScoreAggregate
    episode_summaries: list[EpisodeSummary]
    evidence_items: list[EvidenceReportItem]
    risk_guard_summary: RiskGuardSummary
    badcase_candidates: list[BadcaseItem]
```

报告必须展示：

1. 任务点从哪里来：`source_ref_id/start_line/end_line/quote`。
2. 哪个场景测它：`scenario_id/title/hidden goal 摘要`。
3. 哪轮对话证明失败/通过：`episode_id/turn_id/quote`。
4. 为什么扣分：`judge_point_id/reason/severity`。
5. 是否触发 severity cap。
6. 风险项不是阻塞项时，展示“已识别、已防护、已覆盖”。

#### Agent L：Task Pack / Skill Pack 作为回归资产，而不是内置模板

目标：允许沉淀优秀任务样例和复测资产，但不允许系统通过 task pack 来识别固定业务模板。

当前相关文件：

```text
D:\OutboundEval-meituan\outbound_eval\skills\registry.py
D:\OutboundEval-meituan\eval_skills
```

建议新增目录：

```text
D:\OutboundEval-meituan\task_packs\<slug>\task.md
D:\OutboundEval-meituan\task_packs\<slug>\persona.json
D:\OutboundEval-meituan\task_packs\<slug>\expected_scenarios.json
D:\OutboundEval-meituan\task_packs\<slug>\golden_labels.json
```

必须阅读参考源码：

```text
D:\Public Project\anthropics-skills\skills-main\skills\skill-creator\SKILL.md
D:\Public Project\superpowers-main\skills\using-superpowers\SKILL.md
D:\Public Project\superpowers-main\.codex-plugin\plugin.json
D:\Public Project\BMAD-METHOD-main\src\bmm-skills\module-help.csv
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Primitives\Skill_Catalog.md
E:\obsidian\cache\ApparatusJJ\40_Projects\04-AI_System_Knowledge\Patterns\Skill_Dual_Entry_Pattern.md
```

硬约束：

1. task pack 只能作为 demo、回归测试、人工标注数据。
2. `LLMTaskCompiler` 不能根据 task pack 名称走业务分支。
3. 新任务默认不需要任何 pack，也必须能编译和生成场景。
4. pack 内可存放“人工期望场景”，用于对比 LLM 场景质量，不用于替代 LLM 场景构建。

### 33.23 重构后的端到端接口契约

实现完成后，主链路应从当前：

```text
/api/compile -> /api/qa -> /api/plan -> /api/run
```

升级为：

```text
/api/task/understand
  -> /api/scenarios/build
  -> /api/scenarios/qa
  -> /api/run/start
  -> /api/run/{run_id}/events
  -> /api/report/{run_id}
```

其中 `/api/run` 可以保留为兼容 shortcut，但内部也必须走同一条 runtime pipeline。

推荐状态对象：

```python
class EvaluationRunState(BaseModel):
    run_id: str
    stage: Literal[
        "created", "understanding", "compile_qa", "scenario_build",
        "scenario_qa", "episode_running", "judging", "scoring",
        "reporting", "completed", "failed"
    ]
    task_understanding_id: str | None = None
    scenario_set_id: str | None = None
    episode_ids: list[str] = []
    report_id: str | None = None
    progress: int = 0
    findings: list[CompileFinding | ScenarioFinding | JudgeFinding] = []
```

### 33.24 这次重构必须保留的现有资产

不是所有旧代码都要删。下面资产可以保留，但要降级到正确位置：

| 现有资产 | 保留方式 |
|---|---|
| `section_splitter.py` | 兼容旧测试和 fallback，不再作为主编译入口 |
| `RuleBasedSpecExtractor` | offline fallback，不允许业务关键词命名主导 |
| `CoveragePlanner.DEFAULT_SCENARIO_BLUEPRINTS` | 无 LLM 时的 fallback，不作为默认场景 |
| `action_registry.py` | fallback action schema，不作为用户模拟主输出 |
| `RiskGuardCoverage` | 编译后的 guard 完整性检查和覆盖要求生成 |
| `constraint_checker.py` | 硬规则 guard，如禁词、长度、终止 |
| `knowledge_checker.py` | 可作为简单 grounded hit-rate 辅助信号，不替代 SemanticJudge |
| `ReportGenerator` | 保留渲染能力，输入改为 evidence-first report artifact |

### 33.25 最小可接受但不降级的实施顺序

虽然目标不是最小 MVP，但实现顺序仍要防止多 Agent 互相踩文件。建议按以下顺序派发：

1. Agent A + B：先落 `MarkdownAstParser` 和 `StructuredLLMClient`，这是所有 LLM 模块的地基。
2. Agent C + D：实现 `LLMTaskCompiler`、`TaskUnderstanding`、`JudgePlan/RiskPlan`、`CompileQAGate`。
3. Agent E + J：实现场景构建 API 和前端场景卡片，让评测员能看见并调整场景。
4. Agent F + G：实现用户模拟主路径和 target visibility，保证多轮对话隔离正确。
5. Agent H：实现 `SemanticJudge`，让评分从空实现变成证据驱动。
6. Agent I + K：打通 runtime、trace、report、badcase replay。
7. Agent L：最后沉淀 task pack/golden set，避免早期把样例变成模板。

每个 Agent 完工时必须同时交付：

```text
1. 修改文件列表
2. 新增/更新 schema
3. 单元测试
4. 一个跨行业样例测试，不得只测飞毛腿或直播
5. 说明本模块是否仍有 fallback，fallback 何时启用
```

### 33.26 核心验收标准

本节重构完成后，用下面三类输入验收：

1. 飞毛腿骑手合同通知任务。
2. 课程直播低延迟配置通知任务。
3. 一个全新行业任务，例如“医疗体检预约回访”或“企业软件续费提醒”，任务说明只按 Markdown 层级组织，不使用任何内置模板词。

通过标准：

| 能力 | 验收标准 |
|---|---|
| 通用任务理解 | 三类任务都能生成 `TaskUnderstanding`，且对象都有 source refs |
| 场景构建 | 三类任务都生成贴合任务的场景卡，不是固定 happy/refusal/driving 模板 |
| 用户画像融合 | 评测员画像字段进入 persona、hidden goal、initial utterance |
| 可见边界 | target messages 中无 scenario hidden goal / judge point / risk coverage |
| 语义评分 | 每个 fail/partial 都有 turn evidence 和 reason |
| 报告解释 | 分数可追溯到 judge point、scenario、turn、source markdown node |
| 风险机制 | 风险被识别和防护时不 blocking，缺 guard 才 blocking |
| 可复测 | badcase 可保存并重新运行，golden label 可对比 |

如果系统只能在飞毛腿/直播两个样例上表现好，而在第三个全新行业任务上退化为固定模板或无法评分，则本次重构视为失败。
